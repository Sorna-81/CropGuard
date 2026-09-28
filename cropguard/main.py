"""FastAPI routes for CropGuard's farmer workflow."""
import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from . import config
from .database import connection, initialize_database, reset_database_identity, set_database_identity
from .services import supabase_auth
from .services.prediction import configured_provider, LocalPlantDiseasePredictor
from .services.storage import image_storage
from .services.notifications import in_app_notifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cropguard")

@asynccontextmanager
async def lifespan(application: FastAPI):
    initialize_database()
    yield

app = FastAPI(title="CropGuard", description="Crop disease and pest monitoring", version="2.0.0", docs_url="/api/docs", redoc_url="/api/redoc", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])
config.UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

@app.middleware("http")
async def supabase_auth_context(request: Request, call_next):
    """Verify Supabase access JWTs before setting PostgreSQL RLS claims."""
    if request.url.path.startswith("/api/") and request.method != "OPTIONS":
        client_host = request.client.host if request.client else "unknown"
        cutoff = time.monotonic() - config.RATE_LIMIT_WINDOW_SECONDS
        with _rate_lock:
            hits = _rate_hits.setdefault(client_host, [])
            hits[:] = [stamp for stamp in hits if stamp > cutoff]
            if len(_rate_hits) > 2048:
                for old_host, old_hits in list(_rate_hits.items()):
                    old_hits[:] = [stamp for stamp in old_hits if stamp > cutoff]
                    if not old_hits: _rate_hits.pop(old_host, None)
            if len(hits) >= config.RATE_LIMIT_REQUESTS:
                return JSONResponse(status_code=429, content={"success": False, "message": "Too many requests. Please wait a moment and try again."}, headers={"Retry-After": str(config.RATE_LIMIT_WINDOW_SECONDS)})
            hits.append(time.monotonic())
    if request.url.path in ("/api/predict", "/api/scans"):
        try:
            if int(request.headers.get("content-length", "0")) > config.MAX_UPLOAD_BYTES + 256 * 1024:
                return JSONResponse(status_code=413, content={"success": False, "message": "Image file is too large (maximum 5 MB)"})
        except ValueError:
            return JSONResponse(status_code=400, content={"success": False, "message": "Invalid request size"})
    identity_token = None
    request.state.supabase_user = None
    auth_header = request.headers.get("authorization", "")
    if supabase_auth.enabled() and auth_header.startswith("Bearer "):
        try:
            provider_user = await asyncio.to_thread(supabase_auth.verify, auth_header[7:])
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"success": False, "message": exc.detail})
        if provider_user:
            request.state.supabase_user = provider_user
            identity_token = set_database_identity(provider_user["id"])
    try:
        return await call_next(request)
    finally:
        if identity_token is not None:
            reset_database_identity(identity_token)

_rate_hits = {}
_rate_lock = threading.Lock()

@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed"
    return JSONResponse(status_code=exc.status_code, content={"success": False, "message": message}, headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Preserve useful field-level messages without echoing submitted values.
    errors = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")]
        errors.append({"field": ".".join(location) or "request", "message": str(error.get("msg", "Invalid value"))})
    message = "; ".join(f"{item['field']}: {item['message']}" for item in errors) or "Request validation failed"
    return JSONResponse(status_code=422, content={"success": False, "message": message, "errors": errors})

def now():
    return datetime.now(timezone.utc).isoformat()

def audit(db, actor_id, action, entity_type=None, entity_id=None, details=None):
    """Record an event without credentials or user supplied secrets."""
    db.execute("INSERT INTO audit_log(actor_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
               (actor_id, action, entity_type or "", str(entity_id) if entity_id is not None else None,
                json.dumps(details or {}), now()))

def password_hash(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return "pbkdf2_sha256$310000$%s$%s" % (base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(derived).decode())

def password_matches(password, stored):
    try:
        method, rounds, salt, expected = stored.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return method == "pbkdf2_sha256" and hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False

def token_for(user):
    if not config.JWT_SECRET_KEY:
        raise HTTPException(503, "Authentication is not configured. Set JWT_SECRET_KEY.")
    encode = lambda v: base64.urlsafe_b64encode(json.dumps(v, separators=(",", ":")).encode()).rstrip(b"=").decode()
    header, payload = encode({"alg": "HS256", "typ": "JWT"}), encode({"sub": user["id"], "role": user["role"], "exp": int(time.time()) + 86400})
    body = header + "." + payload
    signature = base64.urlsafe_b64encode(hmac.new(config.JWT_SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return body + "." + signature

def current_user(authorization: Optional[str] = Header(None), request: Request = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue")
    if supabase_auth.enabled():
        provider_user = getattr(request.state, "supabase_user", None) if request else None
        if not provider_user:
            provider_user = supabase_auth.verify(authorization[7:])
        if not provider_user:
            raise HTTPException(401, "Invalid or expired session")
        try:
            with connection() as db:
                row = db.execute("SELECT id,email,role,auth_uid FROM users WHERE auth_uid=?", (provider_user["id"],)).fetchone()
                if not row:
                    row = db.execute("SELECT * FROM public.claim_or_create_user() ").fetchone()
            if not row:
                raise HTTPException(403, "Your verified account could not be provisioned")
            return dict(row)
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Supabase user profile lookup failed (%s, SQLSTATE=%s)", type(exc).__name__, getattr(exc, "sqlstate", "unknown"))
            raise HTTPException(503, "Unable to load your account profile")
    try:
        token = authorization[7:]
        head, payload, signature = token.split(".")
        body = head + "." + payload
        expected = base64.urlsafe_b64encode(hmac.new(config.JWT_SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not config.JWT_SECRET_KEY or not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] < time.time():
            raise ValueError()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with connection() as db:
            if db.execute("SELECT 1 FROM revoked_tokens WHERE token_hash=?", (token_hash,)).fetchone():
                raise ValueError()
            user = db.execute("SELECT id,email,role FROM users WHERE id=?", (data["sub"],)).fetchone()
        if not user:
            raise ValueError()
        return dict(user)
    except Exception:
        raise HTTPException(401, "Invalid or expired session")

def require_role(*roles):
    def dependency(user=Depends(current_user)):
        if user["role"] not in roles:
            raise HTTPException(403, "You do not have permission to access this resource")
        return user
    return dependency

@app.get("/health")
def health():
    try:
        with connection() as db:
            db.execute("SELECT 1")
        database_status = "connected"
    except Exception:
        database_status = "unavailable"
    weather_key = config.get_weather_api_key()
    weather_service = "configured" if weather_key else "not_configured"
    roboflow_disease = bool((config.ROBOFLOW_API_KEY or "").strip() and (config.ROBOFLOW_MODEL_ID or "").strip() and (config.ROBOFLOW_MODEL_VERSION or "").strip())
    roboflow_pest = bool((config.ROBOFLOW_API_KEY or "").strip() and (config.ROBOFLOW_PEST_MODEL_ID or "").strip() and (config.ROBOFLOW_PEST_MODEL_VERSION or "").strip())
    disease_ai = "configured" if (roboflow_disease or config.ENABLE_LOCAL_PREDICTOR) else "not_configured"
    pest_ai = "configured" if (roboflow_pest or config.ENABLE_LOCAL_PREDICTOR) else "not_configured"
    return {
        "status": "healthy" if (database_status == "connected" and disease_ai == "configured") else "degraded",
        "database": database_status,
        "weather_service": weather_service,
        "disease_ai": disease_ai,
        "pest_ai": pest_ai,
    }

@app.get("/api/info")
def info():
    return {"name": "CropGuard", "version": "2.0.0", "features": ["disease_detection", "pest_detection", "weather_risk", "farmer_accounts", "crop_tracking"]}

@app.get("/api/stats")
def public_stats():
    with connection() as db:
        scans_count = db.execute("SELECT count(*) FROM crop_scans").fetchone()[0]
        knowledge_count = db.execute("SELECT (SELECT count(*) FROM diseases)+(SELECT count(*) FROM pests)").fetchone()[0]
        farmers_count = db.execute("SELECT count(*) FROM users WHERE role='FARMER'").fetchone()[0]
    return {"scans": scans_count, "knowledge_entries": knowledge_count, "farmers": farmers_count, "accuracy": None}

@app.post("/api/auth/register")
@app.post("/api/auth/signup")
def register(email: str = Form(...), password: str = Form(...), full_name: str = Form("")):
    if supabase_auth.enabled():
        email = email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(password) < 10:
            raise HTTPException(422, "Enter a valid email and a password of at least 10 characters")
        result = supabase_auth.signup(email, password, full_name.strip())
        user = result.get("user") or {}
        return {"success": True, "user": {"id": user.get("id"), "email": email, "role": "FARMER"}, "access_token": result.get("access_token"), "refresh_token": result.get("refresh_token"), "token_type": result.get("token_type", "bearer"), "expires_in": result.get("expires_in"), "email_confirmation_required": not bool(result.get("access_token")), "message": "Check your email to confirm your account." if not result.get("access_token") else "Account created."}
    if not config.JWT_SECRET_KEY:
        raise HTTPException(503, "Authentication is not configured. Set JWT_SECRET_KEY.")
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(password) < 10:
        raise HTTPException(422, "Enter a valid email and a password of at least 10 characters")
    try:
        with connection() as db:
            cur = db.execute("INSERT INTO users(email,password_hash,role,created_at) VALUES(?,?,?,?)", (email, password_hash(password), "FARMER", now()))
            uid = cur.lastrowid
            db.execute("INSERT INTO farmer_profiles(user_id,full_name) VALUES(?,?)", (uid, full_name.strip()))
            audit(db, uid, "signup", "user", uid)
            user = db.execute("SELECT id,email,role FROM users WHERE id=?", (uid,)).fetchone()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "An account with this email already exists")
        log.error("Registration failed (%s)", type(exc).__name__)
        raise HTTPException(500, "Unable to create account")
    return {"success": True, "user": dict(user), "access_token": token_for(user), "token_type": "bearer"}

@app.post("/api/auth/login")
def login(email: str = Form(...), password: str = Form(...)):
    if supabase_auth.enabled():
        result = supabase_auth.login(email.strip().lower(), password)
        provider_user = result.get("user") or {}
        if not provider_user.get("id"):
            raise HTTPException(401, "Email or password is incorrect")
        identity = set_database_identity(provider_user["id"])
        try:
            with connection() as db:
                user = db.execute("SELECT id,email,role FROM users WHERE auth_uid=?", (provider_user["id"],)).fetchone()
                if not user:
                    user = db.execute("SELECT id,email,role FROM public.claim_or_create_user() ").fetchone()
            if not user:
                raise HTTPException(403, "Your verified account could not be provisioned")
        finally:
            reset_database_identity(identity)
        return {"success": True, "user": dict(user), "access_token": result.get("access_token"), "refresh_token": result.get("refresh_token"), "token_type": result.get("token_type", "bearer"), "expires_in": result.get("expires_in")}
    if not config.JWT_SECRET_KEY:
        raise HTTPException(503, "Authentication is not configured. Set JWT_SECRET_KEY.")
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not user or not password_matches(password, user["password_hash"]):
        raise HTTPException(401, "Email or password is incorrect")
    with connection() as db:
        audit(db, user["id"], "login", "user", user["id"])
    return {"success": True, "user": {"id": user["id"], "email": user["email"], "role": user["role"]}, "access_token": token_for(user), "token_type": "bearer"}

@app.post("/api/auth/logout")
def logout(user=Depends(current_user), authorization: Optional[str] = Header(None)):
    if supabase_auth.enabled() and authorization and authorization.startswith("Bearer "):
        supabase_auth.logout(authorization[7:])
    elif authorization and authorization.startswith("Bearer "):
        raw_token = authorization[7:]
        try:
            _, payload, _ = raw_token.split(".")
            token_data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            expiry = datetime.fromtimestamp(float(token_data["exp"]), timezone.utc).isoformat()
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            with connection() as db:
                db.execute("DELETE FROM revoked_tokens WHERE expires_at<?", (now(),))
                db.execute("INSERT OR IGNORE INTO revoked_tokens(token_hash,expires_at,revoked_at) VALUES(?,?,?)", (token_hash, expiry, now()))
        except Exception:
            raise HTTPException(401, "Invalid or expired session")
    with connection() as db:
        audit(db, user["id"], "logout", "user", user["id"])
    return {"success": True, "message": "Signed out. Remove the access token from this device."}

@app.post("/api/auth/refresh")
def refresh_auth(refresh_token: str = Form(...)):
    if supabase_auth.enabled():
        result = supabase_auth.refresh(refresh_token)
        return {"success": True, "access_token": result.get("access_token"), "refresh_token": result.get("refresh_token"), "token_type": result.get("token_type", "bearer"), "expires_in": result.get("expires_in")}
    raise HTTPException(501, "Session refresh is available with Supabase Auth")

@app.post("/api/auth/password-reset")
def password_reset(email: str = Form(...)):
    if supabase_auth.enabled():
        supabase_auth.recover(email.strip().lower())
        return {"success": True, "message": "If the address is registered, password reset instructions will be sent."}
    raise HTTPException(501, "Password reset requires Supabase Auth configuration")

@app.post("/api/auth/update-password")
def update_password(payload: dict, user=Depends(current_user), authorization: Optional[str] = Header(None)):
    password = str(payload.get("password", ""))
    if len(password) < 10:
        raise HTTPException(422, "Choose a password with at least 10 characters")
    if not supabase_auth.enabled() or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(501, "Password updates from reset links require Supabase Auth")
    supabase_auth.update_password(authorization[7:], password)
    return {"success": True, "message": "Your password has been updated."}

@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user

@app.get("/api/profile")
def get_profile(user=Depends(current_user)):
    with connection() as db:
        row = db.execute("SELECT * FROM farmer_profiles WHERE user_id=?", (user["id"],)).fetchone()
    return dict(row) if row else {}

@app.put("/api/profile")
def update_profile(payload: dict, user=Depends(current_user)):
    fields = ["full_name", "mobile", "state", "district", "village", "language", "farm_area", "soil_type", "irrigation_type"]
    allowed = {k: payload[k] for k in fields if k in payload}
    if not allowed:
        raise HTTPException(422, "Provide at least one profile field")
    with connection() as db:
        db.execute("UPDATE farmer_profiles SET " + ",".join(f"{k}=?" for k in allowed) + " WHERE user_id=?", (*allowed.values(), user["id"]))
        row = db.execute("SELECT * FROM farmer_profiles WHERE user_id=?", (user["id"],)).fetchone()
    return dict(row)

@app.post("/api/crops")
def add_crop(payload: dict, user=Depends(current_user)):
    name = str(payload.get("name", payload.get("crop_name", ""))).strip()
    if not name or len(name) > 80:
        raise HTTPException(422, "Crop name is required")
    keys = ["variety", "sowing_date", "growth_stage", "location", "area", "soil_type", "irrigation_type"]
    with connection() as db:
        cur = db.execute("INSERT INTO crops(user_id,name,variety,sowing_date,growth_stage,location,area,soil_type,irrigation_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (user["id"], name, *(payload.get(k) for k in keys), now()))
        audit(db, user["id"], "crop_created", "crop", cur.lastrowid)
        row = db.execute("SELECT * FROM crops WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)

@app.get("/api/crops")
def list_crops(user=Depends(current_user)):
    with connection() as db:
        return [dict(r) for r in db.execute("SELECT * FROM crops WHERE user_id=? ORDER BY id DESC", (user["id"],))]

@app.get("/api/crops/{crop_id}")
def get_crop(crop_id: int, user=Depends(current_user)):
    with connection() as db:
        row = db.execute("SELECT * FROM crops WHERE id=? AND user_id=?", (crop_id, user["id"])).fetchone()
    if not row: raise HTTPException(404, "Crop not found")
    return dict(row)

@app.put("/api/crops/{crop_id}")
def edit_crop(crop_id: int, payload: dict, user=Depends(current_user)):
    fields = ["name", "variety", "sowing_date", "growth_stage", "location", "area", "soil_type", "irrigation_type"]
    data = {k: payload[k] for k in fields if k in payload}
    if not data: raise HTTPException(422, "Provide at least one crop field")
    with connection() as db:
        cur = db.execute("UPDATE crops SET " + ",".join(f"{k}=?" for k in data) + " WHERE id=? AND user_id=?", (*data.values(), crop_id, user["id"]))
        if cur.rowcount: audit(db, user["id"], "crop_updated", "crop", crop_id)
        row = db.execute("SELECT * FROM crops WHERE id=? AND user_id=?", (crop_id, user["id"])).fetchone()
    if not cur.rowcount: raise HTTPException(404, "Crop not found")
    return dict(row)

@app.delete("/api/crops/{crop_id}")
def remove_crop(crop_id: int, user=Depends(current_user)):
    with connection() as db:
        cur = db.execute("DELETE FROM crops WHERE id=? AND user_id=?", (crop_id, user["id"]))
        if cur.rowcount: audit(db, user["id"], "crop_deleted", "crop", crop_id)
    if not cur.rowcount: raise HTTPException(404, "Crop not found")
    return {"success": True}

_weather_cache = {}
_weather_cache_lock = threading.Lock()

def weather_for(location):
    location = (location or "").strip()
    if not location or len(location) > 150 or any(ord(ch) < 32 for ch in location):
        return None
    api_key = config.get_weather_api_key()
    if not api_key:
        return None
    cache_key = location.casefold()
    with _weather_cache_lock:
        cached = _weather_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < config.WEATHER_CACHE_SECONDS:
        return dict(cached[1])
    try:
        response = None
        candidates = [location]
        cleaned_dist = re.sub(r"(?i)\s+district\b", "", location).strip()
        if cleaned_dist and cleaned_dist not in candidates:
            candidates.append(cleaned_dist)
        if "," in location:
            parts = [p.strip() for p in location.split(",") if p.strip()]
            if parts:
                first = parts[0]
                first_clean = re.sub(r"(?i)\s+district\b", "", first).strip()
                for c in (first, first_clean):
                    if c and c not in candidates:
                        candidates.append(c)
                if len(parts) >= 2 and len(parts[-1]) == 2:
                    code_cand = f"{first_clean},{parts[-1]}"
                    if code_cand not in candidates:
                        candidates.append(code_cand)

        for loc_query in candidates:
            if not loc_query:
                continue
            for attempt in range(2):
                try:
                    response = requests.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"q": loc_query, "appid": api_key, "units": "metric"},
                        timeout=config.WEATHER_TIMEOUT_SECONDS,
                    )
                    if response.status_code >= 500 and attempt == 0:
                        time.sleep(0.1)
                        continue
                    if response.status_code == 200:
                        break
                    elif response.status_code == 404:
                        break
                    else:
                        response.raise_for_status()
                except (requests.Timeout, requests.ConnectionError):
                    if attempt:
                        raise
                    time.sleep(0.1)
            if response and response.status_code == 200:
                break

        if not response or response.status_code != 200:
            log.warning("Weather lookup failed or location not found for: %s", location)
            return None

        data = response.json()
        main_data = data.get("main") or {}
        wind_data = data.get("wind") or {}
        weather_list = data.get("weather") or []

        condition_desc = "Clear"
        if weather_list and isinstance(weather_list, list) and len(weather_list) > 0:
            condition_desc = weather_list[0].get("description", "Clear").capitalize()

        rain_obj = data.get("rain") or {}
        rainfall = None
        if isinstance(rain_obj, dict):
            rainfall = rain_obj.get("1h")
            if rainfall is None:
                rainfall = rain_obj.get("3h")
        elif isinstance(rain_obj, (int, float)):
            rainfall = float(rain_obj)
        if rainfall is not None:
            try:
                rainfall = round(float(rainfall), 2)
            except (ValueError, TypeError):
                rainfall = None

        result = {
            "temperature": round(float(main_data.get("temp", 0)), 1),
            "humidity": int(main_data.get("humidity", 0)),
            "conditions": condition_desc,
            "wind_speed": round(float(wind_data.get("speed", 0)), 1),
            "rainfall": rainfall,
            "location": data.get("name") or location,
        }

        with _weather_cache_lock:
            _weather_cache[cache_key] = (time.monotonic(), result)
            if len(_weather_cache) > 256:
                expired = [key for key, (created, _) in _weather_cache.items() if time.monotonic() - created >= config.WEATHER_CACHE_SECONDS]
                for key in expired: _weather_cache.pop(key, None)
                while len(_weather_cache) > 256:
                    oldest = min(_weather_cache, key=lambda key: _weather_cache[key][0])
                    _weather_cache.pop(oldest, None)
        return dict(result)
    except Exception as exc:
        log.warning("Weather lookup failed for '%s' (%s)", location, type(exc).__name__)
        return None

@app.get("/api/weather")
def weather_endpoint(location: str = Query("", description="City or district name")):
    location = (location or "").strip()
    if not config.get_weather_api_key():
        return {
            "success": False,
            "available": False,
            "message": "Weather service not configured. Add WEATHER_API_KEY in .env to include weather in risk estimates.",
        }
    if not location:
        return {
            "success": False,
            "available": False,
            "message": "Enter a valid location to check local weather.",
        }
    data = weather_for(location)
    if data is None:
        return {
            "success": False,
            "available": False,
            "message": f"Weather data is currently unavailable for '{location}'. Verify location name or check weather service status.",
        }
    return {"success": True, "available": True, "weather_data": data, "timestamp": now()}

@app.get("/test-weather")
def test_weather(location: str = Query("Tirunelveli", description="City or district name")):
    location = (location or "").strip()
    if not config.get_weather_api_key():
        return {"status": "not_configured", "message": "Weather service not configured"}
    if not location:
        return {"status": "unavailable", "message": "Location parameter is required"}
    data = weather_for(location)
    return {"status": "success", "weather_data": data} if data else {"status": "unavailable", "message": f"Weather service is unavailable or location '{location}' was not found"}

@app.get("/test-roboflow")
def test_roboflow():
    configured = bool(config.ROBOFLOW_API_KEY and config.ROBOFLOW_MODEL_ID and config.ROBOFLOW_MODEL_VERSION)
    return {"status": "configured" if configured else "not_configured", "message": "Disease inference provider configuration status; no credentials are returned."}

def assess_risk(name, confidence, weather, previous_count=0):
    score, reasons = 0, []
    if name.lower() == "healthy":
        reasons.append("The model did not identify a disease or pest")
    elif confidence < 0.55:
        reasons.append("Prediction confidence is low; confirm with a clearer image or expert")
        score += 10
    else:
        score += 30
        reasons.append("A disease or pest was detected")

    if weather and isinstance(weather, dict):
        humidity = weather.get("humidity")
        rainfall = weather.get("rainfall")
        temperature = weather.get("temperature")
        weather_factors_applied = False

        if humidity is not None:
            try:
                hum = float(humidity)
                if hum >= 80:
                    score += 25
                    reasons.append(f"High humidity ({round(hum)}%) favors fungal and bacterial disease spread")
                    weather_factors_applied = True
                elif hum >= 65:
                    score += 12
                    reasons.append(f"Moderate to high humidity ({round(hum)}%) may favor foliar pathogens")
                    weather_factors_applied = True
                else:
                    reasons.append(f"Humidity ({round(hum)}%) is moderate to low, reducing disease pressure")
            except (ValueError, TypeError):
                pass

        if rainfall is not None:
            try:
                rain = float(rainfall)
                if rain > 0:
                    score += 15
                    reasons.append(f"Recent rainfall ({round(rain, 1)} mm) leaves foliage wet, increasing infection risk")
                    weather_factors_applied = True
            except (ValueError, TypeError):
                pass

        if temperature is not None:
            try:
                temp = float(temperature)
                if temp > 30:
                    score += 8
                    reasons.append(f"High temperature ({round(temp, 1)} °C) may induce heat stress in crops")
                    weather_factors_applied = True
                elif temp < 10:
                    score += 8
                    reasons.append(f"Low temperature ({round(temp, 1)} °C) may cause cold stress or slow growth")
                    weather_factors_applied = True
            except (ValueError, TypeError):
                pass

        if not weather_factors_applied:
            reasons.append("Current weather conditions are within normal ranges for crop health")
    else:
        reasons.append("Weather data unavailable; weather contribution was omitted")

    if previous_count >= 2:
        score += 10
        reasons.append("Repeated scans exist for this crop")
    score = min(score, 100)
    level = "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW"
    return score, level, reasons

@app.post("/api/scans")
@app.post("/api/predict")
async def predict(image: UploadFile = File(...), cropType: str = Form(""), location: str = Form(""), detection_type: str = Form("disease"), crop_id: Optional[int] = Form(None), authorization: Optional[str] = Header(None)):
    if detection_type not in ("disease", "pest"):
        raise HTTPException(422, "Detection type must be disease or pest")
    ext = Path(image.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(415, "Upload a JPG, JPEG, PNG, or WEBP image")
    content = await image.read(config.MAX_UPLOAD_BYTES + 1)
    if not content: raise HTTPException(400, "The uploaded image is empty")
    if len(content) > config.MAX_UPLOAD_BYTES: raise HTTPException(413, "Image file is too large (maximum 5 MB)")
    try:
        from io import BytesIO
        with Image.open(BytesIO(content)) as im:
            if im.format not in {"JPEG", "PNG", "WEBP"}: raise ValueError()
            im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(400, "The uploaded file is not a valid supported image")
    uid, role, crop = None, None, cropType.strip()
    if authorization and authorization.startswith("Bearer "):
        u = current_user(authorization)
        uid = u["id"]
        auth_uid = u.get("auth_uid")
        if crop_id:
            with connection() as db:
                row = db.execute("SELECT * FROM crops WHERE id=? AND user_id=?", (crop_id, uid)).fetchone()
            if not row: raise HTTPException(404, "Crop not found")
            crop, location = row["name"], location or row["location"] or ""
    else:
        auth_uid = None
    model_id, model_version = ((config.ROBOFLOW_PEST_MODEL_ID, config.ROBOFLOW_PEST_MODEL_VERSION) if detection_type == "pest" else (config.ROBOFLOW_MODEL_ID, config.ROBOFLOW_MODEL_VERSION))
    allow_local = config.ENABLE_LOCAL_PREDICTOR
    provider = configured_provider(config.ROBOFLOW_API_KEY, model_id, model_version, allow_local_fallback=allow_local)
    if detection_type == "pest" and not provider.configured:
        return {"status": "unavailable", "message": "Pest detection model is not configured yet.", "type": "pest", "prediction_source": "none"}
    prediction = None
    if provider.configured:
        try:
            prediction = provider.predict(content, crop=crop, detection_type=detection_type)
        except Exception as exc:
            # Do not log provider URLs or exception messages; they may contain credentials.
            log.error("Prediction provider request failed (%s)", type(exc).__name__)
            if allow_local and not isinstance(provider, LocalPlantDiseasePredictor):
                try:
                    prediction = LocalPlantDiseasePredictor().predict(content, crop=crop, detection_type=detection_type)
                except Exception:
                    pass
    if prediction is None:
        return {"status": "unavailable", "message": "Disease detection is not configured or the AI service is unavailable. No prediction was generated.", "type": detection_type, "prediction_source": "none"}
    name, confidence = prediction.class_name, prediction.confidence
    try:
        filename = image_storage.save(content, ext, auth_uid, crop_id, config.UPLOAD_DIR)
    except Exception as exc:
        log.error("Crop image storage failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Image storage is unavailable. The scan was not saved.")
    loc = (location or "").strip()
    if not loc and crop_id:
        try:
            with connection() as db:
                crop_row = db.execute("SELECT location FROM crops WHERE id=?", (crop_id,)).fetchone()
                if crop_row and crop_row["location"]:
                    loc = crop_row["location"].strip()
        except Exception:
            pass
    weather = weather_for(loc)
    weather_message = None
    if not config.get_weather_api_key():
        weather_message = "Weather service not configured. Add WEATHER_API_KEY in .env to include weather in risk estimates."
    elif not loc:
        weather_message = "No location provided for weather lookup."
    elif weather is None:
        weather_message = f"Weather data is currently unavailable for '{loc}'. Risk was calculated without weather data."

    try:
        with connection() as db:
            previous = db.execute("SELECT count(*) FROM crop_scans WHERE user_id=? AND crop_id IS ?", (uid, crop_id)).fetchone()[0] if uid else 0
            score, risk, reasons = assess_risk(name, confidence, weather, previous)
            severity = "LOW" if name.lower() == "healthy" else ("SEVERE" if risk == "HIGH" else "MODERATE")
            cur = db.execute("INSERT INTO crop_scans(user_id,crop_id,filename,kind,name,confidence,severity,risk_level,risk_score,risk_reasons,weather,created_at,model_name,model_version,prediction_timestamp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (uid, crop_id, filename, detection_type, name, confidence, severity, risk, score, json.dumps(reasons), json.dumps(weather or {}), now(), prediction.model_name, prediction.model_version, prediction.prediction_timestamp))
            scan_id = cur.lastrowid
            if not scan_id:
                raise RuntimeError("Failed to obtain scan record ID from database")
            if uid:
                audit(db, uid, "scan_created", "scan", scan_id, {"kind": detection_type, "risk_level": risk})
            if uid and risk == "HIGH":
                title = "High crop health risk"
                message = f"High estimated risk for {crop or 'your crop'}: {name}."
                alert_type = ("weather_risk" if name.lower() == "healthy" else "disease_risk" if detection_type == "disease" else "pest_risk")
                dedupe_crop = f"crop:{crop_id}" if crop_id else f"user:{uid}:unlinked:{crop.lower()}"
                alert_cursor = db.execute("INSERT OR IGNORE INTO alerts(user_id,crop_id,message,created_at,alert_type,severity,title,scan_id,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?)", (uid, crop_id, message, now(), alert_type, "HIGH", title, scan_id, f"{dedupe_crop}:{alert_type}:{name.lower()}:scan:{scan_id}"))
                if alert_cursor.rowcount:
                    audit(db, uid, "alert_generated", "scan", scan_id, {"kind": detection_type})
                    in_app_notifications.send(db, uid, title, message)
            knowledge = db.execute("SELECT * FROM " + ("pests" if detection_type == "pest" else "diseases") + " WHERE lower(name)=lower(?)", (name,)).fetchone()
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Crop scan persistence failed (%s)", type(exc).__name__)
        raise HTTPException(500, "Scan analysis succeeded but could not be saved to your history. Please try again.")

    info = dict(knowledge) if knowledge else {"name": name, "symptoms": [], "prevention": [], "cultural_management": [], "biological_management": [], "chemical_management": ["Consult a local agricultural officer. Follow the label of any locally registered product."]}
    for key in ["crops", "symptoms", "causes", "favorable_conditions", "prevention", "cultural_management", "biological_management", "chemical_management", "severity_indicators"]:
        if key in info:
            try: info[key] = json.loads(info[key])
            except (ValueError, TypeError): pass
    return {"status": "success", "prediction_id": scan_id, "filename": filename, "crop_type": crop, "type": detection_type, "disease_detected": name.lower() != "healthy" if detection_type == "disease" else None, "disease_name": name, "confidence": round(confidence, 4), "confidence_warning": "Low-confidence result. Please capture a clearer image or request expert review." if confidence < .55 else None, "severity": severity, "severity_basis": "Estimated from detection status and transparent weather risk rules; field damage was not visually measured.", "disease_info": info, "weather": weather, "weather_available": weather is not None, "weather_message": weather_message, "risk_level": risk.lower(), "risk_score": score, "risk_reasons": reasons, "timestamp": now(), "model_name": prediction.model_name, "model_version": prediction.model_version, "prediction_timestamp": prediction.prediction_timestamp, "api_status": {"prediction_source": prediction.model_name}}

@app.get("/api/scans")
@app.get("/api/history")
def scans(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(current_user)):
    with connection() as db:
        rows = db.execute("SELECT * FROM crop_scans WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (user["id"], page_size, (page - 1) * page_size)).fetchall()
    output = []
    for r in rows:
        item = dict(r)
        for field in ("weather", "risk_reasons"):
            if isinstance(item.get(field), str):
                try: item[field] = json.loads(item[field])
                except (ValueError, TypeError): pass
        output.append(item)
    return output

@app.get("/api/recommendations")
def scan_recommendations(scan_id: int = Query(..., ge=1), user=Depends(current_user)):
    """Return stored knowledge-base guidance for one of the caller's scans."""
    with connection() as db:
        scan = db.execute("SELECT id,kind,name FROM crop_scans WHERE id=? AND user_id=?", (scan_id, user["id"])).fetchone()
        if not scan:
            raise HTTPException(404, "Scan not found")
        table = "pests" if scan["kind"] == "pest" else "diseases"
        knowledge = db.execute(f"SELECT prevention,cultural_management,biological_management,chemical_management FROM {table} WHERE lower(name)=lower(?)", (scan["name"],)).fetchone()
    def decode_items(value):
        if isinstance(value, list):
            return value
        try:
            decoded = json.loads(value or "[]")
            return decoded if isinstance(decoded, list) else []
        except (TypeError, ValueError):
            return []
    return {"scan_id": scan_id, "recommendations": {key: decode_items(knowledge[key]) for key in ("prevention", "cultural_management", "biological_management", "chemical_management")} if knowledge else {}}

@app.get("/api/scans/{scan_id}")
def scan_detail(scan_id: int, user=Depends(current_user)):
    with connection() as db:
        row = db.execute("SELECT * FROM crop_scans WHERE id=? AND user_id=?", (scan_id, user["id"])).fetchone()
        reviews = db.execute("SELECT status,request_note,response,created_at,reviewed_at FROM expert_reviews WHERE scan_id=? ORDER BY id DESC", (scan_id,)).fetchall()
    if not row: raise HTTPException(404, "Scan not found")
    result = dict(row)
    for field in ("weather", "risk_reasons"):
        if isinstance(result.get(field), str):
            try: result[field] = json.loads(result[field])
            except (ValueError, TypeError): pass
    result["expert_reviews"] = [dict(r) for r in reviews]
    return result

@app.get("/api/scans/{scan_id}/image")
def scan_image(scan_id: int, user=Depends(current_user)):
    with connection() as db:
        if user["role"] == "ADMIN":
            row = db.execute("SELECT * FROM crop_scans WHERE id=?", (scan_id,)).fetchone()
        elif user["role"] == "EXPERT":
            row = db.execute("SELECT cs.* FROM crop_scans cs WHERE cs.id=? AND EXISTS (SELECT 1 FROM expert_reviews er WHERE er.scan_id=cs.id AND ((er.status IN ('pending','assigned') AND (er.expert_id IS NULL OR er.expert_id=?)) OR (er.status='reviewed' AND er.expert_id=?)))", (scan_id, user["id"], user["id"])).fetchone()
        else:
            row = db.execute("SELECT * FROM crop_scans WHERE id=? AND user_id=?", (scan_id, user["id"])).fetchone()
    if not row or not row["filename"]:
        raise HTTPException(404, "Scan image not found")
    try:
        data = image_storage.read(row["filename"])
    except Exception as exc:
        log.error("Crop image retrieval failed (%s)", type(exc).__name__)
        raise HTTPException(404, "Scan image is no longer available")
    return Response(data, media_type=image_storage.mime(Path(row["filename"]).suffix.lower()), headers={"Cache-Control": "private, no-store"})

@app.get("/api/scans/{scan_id}/report.pdf")
def scan_report(scan_id: int, user=Depends(current_user)):
    try:
        from io import BytesIO
        from xml.sax.saxutils import escape
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as PDFImage
        from reportlab.lib import colors
    except ImportError:
        raise HTTPException(503, "PDF reports require the reportlab package")
    with connection() as db:
        row = db.execute("SELECT cs.*,c.name AS crop_name,c.variety,c.location FROM crop_scans cs LEFT JOIN crops c ON c.id=cs.crop_id WHERE cs.id=? AND cs.user_id=?", (scan_id, user["id"])).fetchone()
        if not row and user["role"] == "ADMIN":
            row = db.execute("SELECT cs.*,c.name AS crop_name,c.variety,c.location FROM crop_scans cs LEFT JOIN crops c ON c.id=cs.crop_id WHERE cs.id=?", (scan_id,)).fetchone()
        elif not row and user["role"] == "EXPERT":
            row = db.execute("SELECT cs.*,c.name AS crop_name,c.variety,c.location FROM crop_scans cs LEFT JOIN crops c ON c.id=cs.crop_id WHERE cs.id=? AND EXISTS (SELECT 1 FROM expert_reviews er WHERE er.scan_id=cs.id AND ((er.status IN ('pending','assigned') AND (er.expert_id IS NULL OR er.expert_id=?)) OR (er.status='reviewed' AND er.expert_id=?)))", (scan_id, user["id"], user["id"])).fetchone()
        if not row: raise HTTPException(404, "Scan not found")
        reviews = db.execute("SELECT status,response,created_at,reviewed_at FROM expert_reviews WHERE scan_id=? ORDER BY id DESC", (scan_id,)).fetchall()
        history = db.execute("SELECT name,kind,confidence,severity,risk_level,created_at FROM crop_scans WHERE user_id=? AND crop_id=? ORDER BY created_at DESC LIMIT 5", (row["user_id"], row["crop_id"])).fetchall() if row["user_id"] and row["crop_id"] else []
        knowledge = db.execute("SELECT prevention,cultural_management,biological_management,chemical_management FROM " + ("pests" if row["kind"] == "pest" else "diseases") + " WHERE lower(name)=lower(?)", (row["name"],)).fetchone()
    def decoded(value, fallback):
        if isinstance(value, (list, dict)): return value
        try: return json.loads(value or "")
        except (ValueError, TypeError): return fallback
    reasons = decoded(row["risk_reasons"], [])
    weather = decoded(row["weather"], {})
    styles = getSampleStyleSheet()
    story = [Paragraph("CropGuard crop health report", styles["Title"]), Spacer(1, 8)]
    if row["filename"]:
        try:
            from io import BytesIO
            from PIL import Image as PILImage
            image_bytes = image_storage.read(row["filename"])
            with PILImage.open(BytesIO(image_bytes)) as source:
                width, height = source.size
            ratio = min(430 / width, 280 / height, 1)
            story.append(PDFImage(BytesIO(image_bytes), width=width * ratio, height=height * ratio))
            story.append(Spacer(1, 8))
        except Exception as exc:
            log.warning("Report image unavailable (%s)", type(exc).__name__)
    recommendation_items = []
    if knowledge:
        for key in ("prevention", "cultural_management", "biological_management", "chemical_management"):
            recommendation_items.extend(decoded(knowledge[key], []))
    recommendations_text = "; ".join(str(item) for item in recommendation_items) or "No knowledge-based recommendation is stored for this class. Consult a local agricultural expert."
    values = [
        ("Crop", row["crop_name"] or "Not linked"), ("Variety", row["variety"] or "Not recorded"),
        ("Scan date", row["created_at"]), ("Finding", f"{row['kind'].title()}: {row['name']}"),
        ("Confidence", f"{float(row['confidence'])*100:.1f}%" if row["confidence"] is not None else "Not available"),
        ("Severity estimate", row["severity"] or "Not available"), ("Risk estimate", f"{row['risk_level'] or 'Not available'} ({row['risk_score'] if row['risk_score'] is not None else '—'}/100)"),
        ("Risk factors", "; ".join(map(str, reasons)) or "None recorded"),
        ("Weather snapshot", "; ".join(f"{k}: {v}" for k, v in weather.items()) if weather else "Weather unavailable"),
        ("Recommendations", recommendations_text),
    ]
    for label, value in values:
        story.append(Paragraph(f"<b>{escape(str(label))}:</b> {escape(str(value))}", styles["BodyText"]))
        story.append(Spacer(1, 5))
    if reviews:
        story += [Spacer(1, 8), Paragraph("Expert review", styles["Heading2"])]
        for review in reviews:
            story.append(Paragraph(escape(f"{review['status']} — {review['response'] or 'No response yet'}"), styles["BodyText"]))
    if history:
        story += [Spacer(1, 8), Paragraph("Recent crop scan history", styles["Heading2"])]
        table_data = [["Date", "Detection", "Confidence", "Severity", "Risk"]]
        table_data.extend([[str(r["created_at"]), f"{r['kind']}: {r['name']}", f"{float(r['confidence'])*100:.1f}%" if r["confidence"] is not None else "—", str(r["severity"] or "—"), str(r["risk_level"] or "—")] for r in history])
        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e8f3eb")), ("GRID", (0,0), (-1,-1), .4, colors.lightgrey), ("FONTSIZE", (0,0), (-1,-1), 8), ("VALIGN", (0,0), (-1,-1), "TOP")]))
        story.append(table)
    output = BytesIO()
    SimpleDocTemplate(output, pagesize=letter, title=f"CropGuard scan {scan_id}").build(story)
    return Response(output.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=cropguard-scan-{scan_id}.pdf", "Cache-Control": "private, no-store"})

@app.get("/api/risk/{crop_id}")
def crop_risk(crop_id: int, user=Depends(current_user)):
    with connection() as db:
        crop = db.execute("SELECT * FROM crops WHERE id=? AND user_id=?", (crop_id, user["id"])).fetchone()
        if not crop: raise HTTPException(404, "Crop not found")
        history = db.execute("SELECT name,confidence FROM crop_scans WHERE crop_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (crop_id, user["id"])).fetchone()
        count = db.execute("SELECT count(*) FROM crop_scans WHERE crop_id=? AND user_id=?", (crop_id, user["id"])).fetchone()[0]
    weather = weather_for(crop["location"] or "")
    name = history["name"] if history else "Healthy"
    confidence = history["confidence"] if history and history["confidence"] is not None else 0
    score, level, reasons = assess_risk(name, confidence, weather, count)
    return {"crop_id": crop_id, "risk_score": score, "risk_level": level, "reasons": reasons, "growth_stage": crop["growth_stage"], "timestamp": now()}

@app.get("/api/diseases")
def diseases():
    with connection() as db: rows = db.execute("SELECT * FROM diseases ORDER BY name").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/diseases/{item_id}")
def disease_detail(item_id: int):
    with connection() as db: row = db.execute("SELECT * FROM diseases WHERE id=?", (item_id,)).fetchone()
    if not row: raise HTTPException(404, "Disease not found")
    return dict(row)

@app.get("/api/pests")
def pests():
    with connection() as db: rows = db.execute("SELECT * FROM pests ORDER BY name").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/pests/{item_id}")
def pest_detail(item_id: int):
    with connection() as db: row = db.execute("SELECT * FROM pests WHERE id=?", (item_id,)).fetchone()
    if not row: raise HTTPException(404, "Pest not found")
    return dict(row)

@app.get("/api/alerts")
def alerts(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            """
            SELECT a.*, c.name AS crop_name, cs.name AS disease_name, cs.kind AS scan_kind,
                   cs.severity AS scan_severity, cs.risk_level AS scan_risk_level, cs.confidence AS scan_confidence,
                   cs.risk_reasons AS scan_risk_reasons
            FROM alerts a
            LEFT JOIN crops c ON a.crop_id = c.id
            LEFT JOIN crop_scans cs ON a.scan_id = cs.id
            WHERE a.user_id = ?
            ORDER BY a.id DESC
            LIMIT ? OFFSET ?
            """,
            (user["id"], page_size, (page - 1) * page_size)
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if isinstance(item.get("scan_risk_reasons"), str):
                try:
                    item["scan_risk_reasons"] = json.loads(item["scan_risk_reasons"])
                except (ValueError, TypeError):
                    pass
            result.append(item)
    return result

@app.put("/api/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int, user=Depends(current_user)):
    with connection() as db: cur = db.execute("UPDATE alerts SET is_read=1 WHERE id=? AND user_id=?", (alert_id, user["id"]))
    if not cur.rowcount: raise HTTPException(404, "Alert not found")
    return {"success": True}

@app.get("/api/notifications")
def get_notifications(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user["id"], page_size, (page - 1) * page_size)
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/scans/{scan_id}/expert-review")
def request_review(scan_id: int, payload: dict = {}, user=Depends(current_user)):
    with connection() as db:
        scan = db.execute("SELECT id FROM crop_scans WHERE id=? AND user_id=?", (scan_id, user["id"])).fetchone()
        if not scan: raise HTTPException(404, "Scan not found")
        cur = db.execute("INSERT INTO expert_reviews(scan_id,farmer_id,request_note,created_at) VALUES(?,?,?,?)", (scan_id, user["id"], str(payload.get("note", ""))[:1000], now()))
        audit(db, user["id"], "expert_review_requested", "expert_review", cur.lastrowid, {"scan_id": scan_id})
        return {"id": cur.lastrowid, "status": "pending"}

@app.get("/api/expert/cases")
def expert_cases(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(require_role("EXPERT", "ADMIN"))):
    with connection() as db: rows = db.execute("SELECT er.*,cs.name,cs.kind,cs.confidence,cs.filename FROM expert_reviews er JOIN crop_scans cs ON cs.id=er.scan_id WHERE (?='ADMIN' OR er.status='pending' OR (er.status='assigned' AND (er.expert_id IS NULL OR er.expert_id=?)) OR er.expert_id=?) ORDER BY er.id DESC LIMIT ? OFFSET ?", (user["role"], user["id"], user["id"], page_size, (page - 1) * page_size)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/expert/cases/{case_id}")
def expert_case(case_id: int, user=Depends(require_role("EXPERT", "ADMIN"))):
    with connection() as db:
        row = db.execute("SELECT er.*,cs.name,cs.kind,cs.confidence,cs.filename,cs.weather,cs.risk_level,cs.risk_reasons,cs.crop_id,cs.user_id FROM expert_reviews er JOIN crop_scans cs ON cs.id=er.scan_id WHERE er.id=? AND (?='ADMIN' OR er.status='pending' OR (er.status='assigned' AND (er.expert_id IS NULL OR er.expert_id=?)) OR er.expert_id=?)", (case_id, user["role"], user["id"], user["id"])).fetchone()
    if not row: raise HTTPException(404, "Review case not found")
    result = dict(row)
    if result.get("crop_id"):
        with connection() as db:
            previous = db.execute("SELECT id,name,kind,confidence,severity,risk_level,risk_reasons,created_at FROM crop_scans WHERE crop_id=? AND user_id=? AND id<>? ORDER BY created_at DESC LIMIT 5", (result["crop_id"], result["user_id"], result["scan_id"])).fetchall()
        result["previous_scans"] = [dict(item) for item in previous]
    else:
        result["previous_scans"] = []
    return result

@app.post("/api/expert/cases/{case_id}/review")
def review_case(case_id: int, payload: dict, user=Depends(require_role("EXPERT", "ADMIN"))):
    response = str(payload.get("response", "")).strip()
    if not response: raise HTTPException(422, "Review response is required")
    with connection() as db:
        cur = db.execute("UPDATE expert_reviews SET expert_id=?,response=?,status='reviewed',reviewed_at=? WHERE id=? AND status IN ('pending','assigned') AND (expert_id IS NULL OR expert_id=? OR ?='ADMIN')", (user["id"], response[:4000], now(), case_id, user["id"], user["role"]))
        if cur.rowcount: audit(db, user["id"], "expert_review_completed", "expert_review", case_id)
    if not cur.rowcount: raise HTTPException(404, "Review case not found")
    return {"success": True, "status": "reviewed"}

@app.get("/api/admin/dashboard")
def admin_dashboard(user=Depends(require_role("ADMIN"))):
    with connection() as db:
        risk = db.execute("SELECT risk_level,count(*) AS total FROM crop_scans WHERE risk_level IS NOT NULL GROUP BY risk_level").fetchall()
        detections = db.execute("SELECT kind,name,count(*) AS total FROM crop_scans GROUP BY kind,name ORDER BY total DESC LIMIT 8").fetchall()
        review_status = db.execute("SELECT status,count(*) AS total FROM expert_reviews GROUP BY status").fetchall()
        activity = db.execute("SELECT action,entity_type,entity_id,created_at FROM audit_log ORDER BY id DESC LIMIT 10").fetchall()
        return {"farmers": db.execute("SELECT count(*) FROM users WHERE role='FARMER'").fetchone()[0], "crops": db.execute("SELECT count(*) FROM crops").fetchone()[0], "scans": db.execute("SELECT count(*) FROM crop_scans").fetchone()[0], "pending_reviews": db.execute("SELECT count(*) FROM expert_reviews WHERE status IN ('pending','assigned')").fetchone()[0], "risk_distribution": {r["risk_level"]: r["total"] for r in risk}, "detection_distribution": [dict(r) for r in detections], "review_status": {r["status"]: r["total"] for r in review_status}, "recent_activity": [dict(r) for r in activity]}

@app.get("/api/admin/farmers")
def admin_farmers(page: int = Query(1, ge=1), page_size: int = Query(1, ge=1, le=200), user=Depends(require_role("ADMIN"))):
    with connection() as db: rows = db.execute("SELECT u.id,u.email,u.created_at,p.full_name,p.state,p.district FROM users u LEFT JOIN farmer_profiles p ON p.user_id=u.id WHERE u.role='FARMER' ORDER BY u.id DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/scans")
def admin_scans(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(require_role("ADMIN"))):
    with connection() as db: rows = db.execute("SELECT * FROM crop_scans ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size)).fetchall()
    return [dict(r) for r in rows]

def write_knowledge(table, payload, item_id=None, actor_id=None):
    fields = ["name", "scientific_name", "crops", "symptoms", "causes", "favorable_conditions", "prevention", "cultural_management", "biological_management", "chemical_management", "severity_indicators"]
    data = {key: payload[key] for key in fields if key in payload}
    if not item_id and not data.get("name"):
        raise HTTPException(422, "A name is required")
    if not data:
        raise HTTPException(422, "Provide at least one knowledge field")
    for key, value in list(data.items()):
        if key not in ("name", "scientific_name"):
            data[key] = json.dumps(value if isinstance(value, list) else [])
    try:
        with connection() as db:
            if item_id:
                cur = db.execute(f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in data) + " WHERE id=?", (*data.values(), item_id))
                if not cur.rowcount: raise HTTPException(404, "Knowledge entry not found")
                row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
            else:
                columns = list(data)
                cur = db.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(data.values()))
                row = db.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone()
            if actor_id is not None:
                audit(db, actor_id, "admin_knowledge_updated" if item_id else "admin_knowledge_created", table, item_id or cur.lastrowid)
        return dict(row)
    except HTTPException:
        raise
    except Exception:
        log.error("Knowledge base update failed (request rejected)")
        raise HTTPException(409, "Unable to save this knowledge entry")

def delete_knowledge(table, item_id, actor_id=None):
    with connection() as db:
        cur = db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        if cur.rowcount and actor_id is not None: audit(db, actor_id, "admin_knowledge_deleted", table, item_id)
    if not cur.rowcount: raise HTTPException(404, "Knowledge entry not found")
    return {"success": True}

@app.post("/api/admin/diseases")
def admin_add_disease(payload: dict, user=Depends(require_role("ADMIN"))): return write_knowledge("diseases", payload, actor_id=user["id"])

@app.put("/api/admin/diseases/{item_id}")
def admin_edit_disease(item_id: int, payload: dict, user=Depends(require_role("ADMIN"))): return write_knowledge("diseases", payload, item_id, user["id"])

@app.delete("/api/admin/diseases/{item_id}")
def admin_delete_disease(item_id: int, user=Depends(require_role("ADMIN"))): return delete_knowledge("diseases", item_id, user["id"])

@app.post("/api/admin/pests")
def admin_add_pest(payload: dict, user=Depends(require_role("ADMIN"))): return write_knowledge("pests", payload, actor_id=user["id"])

@app.put("/api/admin/pests/{item_id}")
def admin_edit_pest(item_id: int, payload: dict, user=Depends(require_role("ADMIN"))): return write_knowledge("pests", payload, item_id, user["id"])

@app.delete("/api/admin/pests/{item_id}")
def admin_delete_pest(item_id: int, user=Depends(require_role("ADMIN"))): return delete_knowledge("pests", item_id, user["id"])

@app.get("/")
def home(): return FileResponse(str(config.STATIC_DIR / "index.html"))

@app.get("/{full_path:path}")
def frontend_fallback(full_path: str):
    if full_path.startswith("api/") or full_path == "health": raise HTTPException(404, "Not found")
    return FileResponse(str(config.STATIC_DIR / "index.html"))

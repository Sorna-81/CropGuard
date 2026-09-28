"""Environment-backed application settings."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / ".env"

if not env_path.exists():
    example_path = BASE_DIR / ".env.example"
    if example_path.exists():
        example_content = example_path.read_text(encoding="utf-8")
        random_secret = secrets.token_urlsafe(48)
        new_content = example_content.replace(
            "JWT_SECRET_KEY=replace-with-a-random-secret-of-at-least-32-bytes",
            f"JWT_SECRET_KEY={random_secret}"
        )
        if "JWT_SECRET_KEY=" not in new_content or random_secret not in new_content:
            new_content += f"\nJWT_SECRET_KEY={random_secret}\n"
        try:
            env_path.write_text(new_content, encoding="utf-8")
        except OSError:
            pass

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=env_path)
except ImportError:
    # Keep .env configuration functional in offline/minimal local installs.
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'cropguard.db').as_posix()}")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", os.getenv("SUPABASE_ANON_KEY", ""))
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
SUPABASE_CA_BUNDLE = os.getenv("SUPABASE_CA_BUNDLE") or os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or None
SUPABASE_PROXY = os.getenv("SUPABASE_PROXY", "")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "crop-images")
SUPABASE_AUTH_ENABLED = bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY)
SUPABASE_DATABASE_ENABLED = DATABASE_URL.startswith(("postgresql://", "postgres://"))
ROBOFLOW_API_KEY = os.getenv("ROBOFLOW_API_KEY", "")
ROBOFLOW_MODEL_ID = os.getenv("ROBOFLOW_MODEL_ID", "")
ROBOFLOW_MODEL_VERSION = os.getenv("ROBOFLOW_MODEL_VERSION", "")
ROBOFLOW_PEST_MODEL_ID = os.getenv("ROBOFLOW_PEST_MODEL_ID", "")
ROBOFLOW_PEST_MODEL_VERSION = os.getenv("ROBOFLOW_PEST_MODEL_VERSION", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
ENABLE_OPEN_METEO = os.getenv("ENABLE_OPEN_METEO", "true").lower() in ("true", "1", "yes")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not JWT_SECRET_KEY and not SUPABASE_DATABASE_ENABLED:
    JWT_SECRET_KEY = secrets.token_urlsafe(48)
ALLOWED_ORIGINS = [v.strip() for v in os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if v.strip()]
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", "600"))
WEATHER_TIMEOUT_SECONDS = float(os.getenv("WEATHER_TIMEOUT_SECONDS", "8"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
ENABLE_LOCAL_PREDICTOR = os.getenv("ENABLE_LOCAL_PREDICTOR", "true").lower() in ("true", "1", "yes")

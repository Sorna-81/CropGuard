"""SQLite development and Supabase PostgreSQL persistence with request-scoped RLS claims."""
import json
import re
import sqlite3
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from . import config
from .config import BASE_DIR, DATABASE_URL

if DATABASE_URL.startswith("sqlite:///"):
    DB_PATH = Path(DATABASE_URL.removeprefix("sqlite:///"))
    if not DB_PATH.is_absolute():
        DB_PATH = BASE_DIR / DB_PATH
else:
    DB_PATH = BASE_DIR / "cropguard.db"

_database_identity = ContextVar("cropguard_database_identity", default=None)

def set_database_identity(auth_uid):
    """Set verified Supabase Auth user ID for RLS within this request context."""
    return _database_identity.set(str(auth_uid) if auth_uid else None)

def reset_database_identity(token):
    _database_identity.reset(token)

class PostgresCursor:
    """Translate the small sqlite-style query API used by the existing routes."""
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    @property
    def rowcount(self): return self.cursor.rowcount

    def execute(self, statement, parameters=()):
        sql = statement.strip()
        ignored = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", sql))
        if ignored:
            sql = re.sub(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", sql)
            sql += " ON CONFLICT DO NOTHING"
        json_columns = {"crops", "symptoms", "causes", "favorable_conditions", "prevention", "cultural_management", "biological_management", "chemical_management", "severity_indicators", "risk_reasons", "weather", "data", "reasons", "details"}
        date_columns = {"sowing_date"}
        timestamp_columns = {"created_at", "updated_at", "reviewed_at", "prediction_timestamp", "sent_at"}
        numeric_columns = {"area", "farm_area"}
        insert_match = re.search(r"(?is)\bINSERT\s+INTO\s+(?:public\.)?\w+\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", sql)
        if insert_match:
            columns = [value.strip().strip('"') for value in insert_match.group(1).split(",")]
            values = [value.strip() for value in insert_match.group(2).split(",")]
            def typed_placeholder(col, value):
                if value != "?": return value
                if col in json_columns: return "?::jsonb"
                if col in date_columns: return "?::date"
                if col in timestamp_columns: return "?::timestamptz"
                if col in numeric_columns: return "?::numeric"
                return value
            values = [typed_placeholder(col, value) for col, value in zip(columns, values)]
            sql = sql[:insert_match.start(2)] + ",".join(values) + sql[insert_match.end(2):]
        update_match = re.search(r"(?is)\bUPDATE\s+(?:public\.)?\w+\s+SET\s+(.*?)\s+WHERE\s", sql)
        if update_match:
            assignments = update_match.group(1)
            casts = {**{c: "jsonb" for c in json_columns}, **{c: "date" for c in date_columns}, **{c: "timestamptz" for c in timestamp_columns}, **{c: "numeric" for c in numeric_columns}}
            for column, cast in casts.items():
                assignments = re.sub(rf"(?i)\b{column}\s*=\s*\?", f"{column}=?::{cast}", assignments)
            sql = sql[:update_match.start(1)] + assignments + sql[update_match.end(1):]
        sql = re.sub(r"(?is)\s+IS\s+\?", " IS NOT DISTINCT FROM ?", sql)
        sql = sql.replace("?", "%s")
        is_insert = bool(re.match(r"(?is)^INSERT\s+INTO\s+", sql))
        if is_insert and " RETURNING " not in sql.upper():
            sql += " RETURNING id"
        self.cursor.execute(sql, parameters)
        if is_insert:
            inserted = self.cursor.fetchone()
            self.lastrowid = inserted.get("id") if inserted else None
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        return PostgresRow(row) if row is not None else None
    def fetchall(self):
        rows = self.cursor.fetchall()
        return [PostgresRow(row) for row in rows]

    def __iter__(self):
        # psycopg cursors do not expose SQLite's implicit cursor iteration API.
        # Keep the existing route contract while consuming only this result set.
        return iter(self.fetchall())

class PostgresRow(dict):
    """Mapping row compatible with the SQLite rows used by the existing app."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)

class PostgresConnection:
    def __init__(self, raw): self.raw = raw
    def execute(self, statement, parameters=()):
        from psycopg.rows import dict_row
        return PostgresCursor(self.raw.cursor(row_factory=dict_row)).execute(statement, parameters)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'FARMER', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS farmer_profiles(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE, full_name TEXT NOT NULL DEFAULT '', mobile TEXT, state TEXT, district TEXT, village TEXT, language TEXT DEFAULT 'English', farm_area REAL, soil_type TEXT, irrigation_type TEXT);
CREATE TABLE IF NOT EXISTS crops(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, variety TEXT, sowing_date TEXT, growth_stage TEXT, location TEXT, area REAL, soil_type TEXT, irrigation_type TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diseases(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, scientific_name TEXT, crops TEXT DEFAULT '[]', symptoms TEXT DEFAULT '[]', causes TEXT DEFAULT '[]', favorable_conditions TEXT DEFAULT '[]', prevention TEXT DEFAULT '[]', cultural_management TEXT DEFAULT '[]', biological_management TEXT DEFAULT '[]', chemical_management TEXT DEFAULT '[]', severity_indicators TEXT DEFAULT '[]', kind TEXT DEFAULT 'disease');
CREATE TABLE IF NOT EXISTS pests(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, scientific_name TEXT, crops TEXT DEFAULT '[]', symptoms TEXT DEFAULT '[]', causes TEXT DEFAULT '[]', favorable_conditions TEXT DEFAULT '[]', prevention TEXT DEFAULT '[]', cultural_management TEXT DEFAULT '[]', biological_management TEXT DEFAULT '[]', chemical_management TEXT DEFAULT '[]', severity_indicators TEXT DEFAULT '[]', kind TEXT DEFAULT 'pest');
CREATE TABLE IF NOT EXISTS recommendations(id INTEGER PRIMARY KEY, crop_scan_id INTEGER, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weather_records(id INTEGER PRIMARY KEY, location TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crop_scans(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, crop_id INTEGER REFERENCES crops(id) ON DELETE SET NULL, filename TEXT, kind TEXT NOT NULL, name TEXT NOT NULL, confidence REAL, severity TEXT, risk_level TEXT, risk_score INTEGER, risk_reasons TEXT DEFAULT '[]', weather TEXT DEFAULT '{}', created_at TEXT NOT NULL, model_name TEXT, model_version TEXT, prediction_timestamp TEXT);
CREATE TABLE IF NOT EXISTS risk_assessments(id INTEGER PRIMARY KEY, crop_scan_id INTEGER REFERENCES crop_scans(id) ON DELETE CASCADE, score INTEGER NOT NULL, level TEXT NOT NULL, reasons TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, crop_id INTEGER REFERENCES crops(id) ON DELETE CASCADE, message TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, alert_type TEXT DEFAULT 'risk', severity TEXT, title TEXT, scan_id INTEGER, dedupe_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS expert_reviews(id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES crop_scans(id) ON DELETE CASCADE, farmer_id INTEGER REFERENCES users(id), expert_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'pending', request_note TEXT, response TEXT, created_at TEXT NOT NULL, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL, entity_type TEXT, entity_id TEXT, details TEXT DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, channel TEXT NOT NULL DEFAULT 'in_app', status TEXT NOT NULL DEFAULT 'sent', title TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT);
CREATE TABLE IF NOT EXISTS revoked_tokens(token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, revoked_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_scans_user_created ON crop_scans(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, is_read);
"""

@contextmanager
def connection():
    if DATABASE_URL.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL is configured but psycopg is missing. Install requirements.txt.") from exc
        db_url = DATABASE_URL
        if "sslmode=" not in db_url:
            db_url += "&sslmode=require" if "?" in db_url else "?sslmode=require"
        auth_uid = _database_identity.get()
        raw = psycopg.connect(db_url, row_factory=dict_row)
        try:
            with raw.transaction():
                # Apply caller identity before each query. RLS, rather than UI checks,
                # restricts this role to the verified Supabase Auth subject.
                raw.execute("SET LOCAL ROLE " + ("authenticated" if auth_uid else "anon"))
                claims = {"sub": auth_uid, "role": "authenticated" if auth_uid else "anon"}
                raw.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
                yield PostgresConnection(raw)
        finally:
            raw.close()
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def initialize_database():
    if DATABASE_URL.startswith(("postgresql://", "postgres://")):
        # Production schemas are created only by checked-in Supabase migrations.
        if not (config.SUPABASE_URL and config.SUPABASE_PUBLISHABLE_KEY and config.SUPABASE_SECRET_KEY):
            raise RuntimeError("Supabase mode requires SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, and server-only SUPABASE_SECRET_KEY")
        with connection() as db:
            db.execute("SELECT 1")
        return
    with connection() as db:
        db.executescript(SCHEMA)
        # Additive upgrades for databases created by earlier CropGuard versions.
        upgrades = {
            "crop_scans": {"model_name": "TEXT", "model_version": "TEXT", "prediction_timestamp": "TEXT"},
            "alerts": {"alert_type": "TEXT DEFAULT 'risk'", "severity": "TEXT", "title": "TEXT", "scan_id": "INTEGER", "dedupe_key": "TEXT"},
        }
        for table, columns in upgrades.items():
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, declaration in columns.items():
                if column not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        notification_columns = {row[1] for row in db.execute("PRAGMA table_info(notifications)").fetchall()}
        for column in ("title", "message"):
            if column not in notification_columns:
                db.execute(f"ALTER TABLE notifications ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if "sent_at" not in notification_columns:
            db.execute("ALTER TABLE notifications ADD COLUMN sent_at TEXT")
        seeds = [
            ("Early Blight", "Alternaria solani", ["Tomato", "Potato"], ["Concentric dark leaf lesions", "Yellowing"], ["Remove affected foliage", "Rotate crops", "Keep foliage dry"], "disease"),
            ("Late Blight", "Phytophthora infestans", ["Tomato", "Potato"], ["Water-soaked lesions", "White growth in humid conditions"], ["Remove affected plants safely", "Improve drainage", "Seek local extension advice promptly"], "disease"),
            ("Leaf Mold", "Passalora fulva", ["Tomato"], ["Yellow upper leaf spots", "Olive growth beneath leaves"], ["Improve airflow", "Avoid overhead irrigation"], "disease"),
            ("Bacterial Spot", "Xanthomonas spp.", ["Tomato", "Chilli"], ["Small dark spots, sometimes with yellow halos"], ["Use clean seed", "Avoid handling wet plants", "Rotate crops"], "disease"),
            ("Powdery Mildew", "Various fungi", ["Tomato", "Wheat", "Chilli"], ["White powdery growth"], ["Improve spacing", "Remove affected leaves"], "disease"),
            ("Healthy", "", ["Tomato", "Potato", "Rice", "Wheat", "Maize", "Cotton", "Soybean", "Sugarcane", "Onion", "Chilli"], ["No visible disease symptoms"], ["Continue monitoring and sound crop care"], "disease"),
            ("Aphids", "Aphidoidea", ["Tomato", "Cotton", "Chilli"], ["Clusters of small insects", "Sticky honeydew", "Curled leaves"], ["Inspect leaf undersides", "Conserve beneficial insects", "Consult local extension guidance"], "pest"),
            ("Fall Armyworm", "Spodoptera frugiperda", ["Maize"], ["Windowing or ragged feeding on leaves", "Frass in whorl"], ["Scout whorls regularly", "Use locally recommended integrated pest management"], "pest")]
        for name, scientific, crops, symptoms, management, kind in seeds:
            table = "pests" if kind == "pest" else "diseases"
            db.execute(f"INSERT OR IGNORE INTO {table}(name,scientific_name,crops,symptoms,prevention,cultural_management,biological_management,chemical_management,severity_indicators,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (name, scientific, __import__('json').dumps(crops), __import__('json').dumps(symptoms), __import__('json').dumps(management), __import__('json').dumps(management), __import__('json').dumps(["Consult a local agricultural extension officer about locally registered options."]), __import__('json').dumps(["Use only products registered for the crop and follow the label; consult local agricultural experts."]), __import__('json').dumps(["Assess affected plant area and spread with local expert advice."]), kind))

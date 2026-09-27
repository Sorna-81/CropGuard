"""One-time, resumable data copy from the local SQLite file to Supabase Postgres.

Run after `supabase db push`, with DATABASE_URL pointing to the Supabase direct
database connection. User password hashes are deliberately omitted; legacy users
must set a new Supabase Auth password using their verified existing email.
"""
import os
import sqlite3
from pathlib import Path

import psycopg

from cropguard.config import BASE_DIR, DATABASE_URL

TABLES = (
    "users", "farmer_profiles", "crops", "diseases", "pests", "crop_scans",
    "recommendations", "weather_records", "risk_assessments", "alerts", "expert_reviews", "audit_log",
)
JSON_COLUMNS = {"crops", "symptoms", "causes", "favorable_conditions", "prevention", "cultural_management",
                "biological_management", "chemical_management", "severity_indicators", "risk_reasons", "weather", "data", "reasons", "details"}


def main():
    source = Path(os.getenv("SQLITE_SOURCE", BASE_DIR / "cropguard.db"))
    target = os.getenv("DATABASE_URL", DATABASE_URL)
    if target.startswith("sqlite:"):
        raise SystemExit("Set DATABASE_URL to the Supabase PostgreSQL connection before running this script")
    if not source.exists():
        raise SystemExit(f"SQLite source file was not found: {source}")
    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        with psycopg.connect(target) as dst:
            with dst.cursor() as cur:
                for table in TABLES:
                    exists = src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if not exists:
                        continue
                    source_columns = [row[1] for row in src.execute(f'PRAGMA table_info("{table}")')]
                    cur.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))
                    target_types = dict(cur.fetchall())
                    columns = [column for column in source_columns if column in target_types and not (table == "users" and column == "password_hash")]
                    if table == "users" and "password_hash" in target_types:
                        columns.append("password_hash")
                    if not columns:
                        continue
                    quoted = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
                    values = []
                    for row in src.execute(f'SELECT * FROM "{table}"'):
                        item = dict(row)
                        record = []
                        for col in columns:
                            val = None if table == "users" and col == "password_hash" else item.get(col)
                            record.append(val)
                        values.append(record)
                    if not values:
                        continue
                    casts = []
                    for col in columns:
                        dtype = target_types.get(col, "")
                        casts.append("%s::jsonb" if dtype == "jsonb" else "%s::date" if dtype == "date" else "%s::timestamptz" if dtype in ("timestamp with time zone", "timestamp without time zone") else "%s")
                    sql = f'INSERT INTO public."{table}" ({quoted}) VALUES ({",".join(casts)}) ON CONFLICT DO NOTHING'
                    cur.executemany(sql, values)
                    print(f"{table}: copied {len(values)} source rows (existing IDs were retained)")
                    if "id" in columns:
                        cur.execute(f"SELECT setval(pg_get_serial_sequence('public.{table}','id'), greatest(coalesce((select max(id) from public.{table}),1),1), true)")
            dst.commit()
    finally:
        src.close()
    print("Copy complete. Legacy passwords were not copied; users must confirm their existing email and set a Supabase Auth password.")


if __name__ == "__main__":
    main()

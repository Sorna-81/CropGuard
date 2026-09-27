# CropGuard — Plant Disease Identification

CropGuard is an existing FastAPI and HTML application for early detection and management of crop diseases and pest infestations.

The app preserves its upload page and Roboflow workflow. It adds farmer accounts, crop tracking, persistent scan data, weather-aware risk estimates, alerts, knowledge entries, and an expert review workflow. It does not claim model accuracy or generate mock predictions.

## Current capabilities

- FastAPI app and existing responsive Bootstrap page.
- Configurable Roboflow disease inference; pest inference is supported when pest model settings are configured.
- Image checks for JPG, JPEG, PNG, and WEBP, valid image content, empty files, and a 5 MB maximum.
- Clear unavailable response when an inference model is missing or unreachable.
- Optional OpenWeatherMap lookup by user supplied location. Weather failure does not stop image analysis.
- Transparent risk score and reasons using prediction, humidity, rainfall, temperature, and repeat crop scans.
- Farmer registration/login, signed bearer tokens, PBKDF2 password hashing, farmer profile, and crop CRUD.
- SQLite records for users, profiles, crops, disease/pest knowledge, scans, weather, recommendations, risk assessments, alerts, and expert reviews.
- Role checks for farmer, expert, and admin routes.
- Seeded common knowledge entries for early blight, late blight, leaf mold, bacterial spot, powdery mildew, healthy plants, aphids, and fall armyworm.
- Health, info, and live database-backed public counts. Accuracy is shown as unevaluated.
- Frontend registration, login/logout, sessionStorage JWT handling, role navigation, profile, crops, scan, saved history, timelines, alerts, expert reviews, admin metrics, and knowledge editing.

## Architecture

```text
main.py                 Local run entry point
cropguard/
  config.py              Environment configuration
  database.py            SQLite schema, connection helper, seed data
  main.py                FastAPI application and API routes
static/index.html        Responsive CropGuard interface
static/app.js             API client, auth session, role UI, and page flows
uploads/                 Local generated image storage
```

SQLite remains the zero-configuration local development default. A PostgreSQL adapter and checked-in Supabase migration are available for a configured Supabase deployment. Supabase Auth and private image storage are enabled only when the required Supabase settings and PostgreSQL URL are present. Supabase has not been provisioned or live-tested by this repository change.

## Setup on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env`. Set `JWT_SECRET_KEY` to a long random value. For example, PowerShell can generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated value in `.env`; do not commit that file. Configure the disease model and weather key if those services are available. Leave pest settings blank until a real pest model is ready.

Start the app:

```powershell
python main.py
```

Open `http://127.0.0.1:8000`; interactive API docs are at `/api/docs`. The database is initialized and common knowledge entries are seeded at first startup. The API binds to localhost for local development.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `ROBOFLOW_API_KEY` | Server-side Roboflow credential |
| `ROBOFLOW_MODEL_ID` | Disease model identifier |
| `ROBOFLOW_MODEL_VERSION` | Disease model version |
| `ROBOFLOW_PEST_MODEL_ID` | Optional pest model identifier |
| `ROBOFLOW_PEST_MODEL_VERSION` | Optional pest model version |
| `WEATHER_API_KEY` | Optional OpenWeatherMap credential |
| `JWT_SECRET_KEY` | Signs bearer tokens; required for login/register |
| `DATABASE_URL` | SQLite URL, defaults to `sqlite:///cropguard.db` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `SUPABASE_URL` | Supabase project URL (server-side) |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase Auth publishable/anon key |
| `SUPABASE_SECRET_KEY` | Server-only Storage key; never expose in JavaScript |
| `SUPABASE_STORAGE_BUCKET` | Private image bucket; defaults to `crop-images` |
| `WEATHER_CACHE_SECONDS` | In-memory current-weather cache duration |
| `WEATHER_TIMEOUT_SECONDS` | Weather request timeout |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | Per-process API rate limit |

Keys are only read on the backend. Health responses disclose configuration status without returning key values.

## Main API routes

- `GET /health`, `GET /api/info`, `GET /api/stats`
- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- `GET /api/profile`, `PUT /api/profile`
- `POST /api/crops`, `GET /api/crops`, `GET|PUT|DELETE /api/crops/{id}`
- `POST /api/predict` (multipart `image`, `cropType`, `location`, optional `detection_type` and authenticated `crop_id`)
- `GET /api/scans`, `GET /api/scans/{id}`
- `GET /api/diseases`, `GET /api/diseases/{id}`, and corresponding pest routes
- `GET /api/weather?location=...`
- `GET /api/alerts`, `PUT /api/alerts/{id}/read`
- `POST /api/scans/{id}/expert-review`
- `GET /api/scans/{id}/image`, `GET /api/scans/{id}/report.pdf`
- `GET /api/expert/cases`, `POST /api/expert/cases/{id}/review`
- `GET /api/admin/dashboard`, `GET /api/admin/farmers`, `GET /api/admin/scans`

Authenticated routes expect `Authorization: Bearer <access_token>`. New public registrations always receive the FARMER role. Create and promote expert/admin accounts through a controlled database administration process; there are no demo passwords in source.

## Risk and AI behavior

Roboflow is the only configured image inference provider. If its credentials/model are missing or inference fails, the API reports that analysis is unavailable; it does not invent a class or confidence. Pest detection returns “Pest detection model is not configured yet.” until a pest model is supplied.

Risk is a heuristic score with returned reasons, not a validated agronomic forecast. It adds factors for detection confidence, humidity, rainfall, temperature extremes, and repeated scans. Risk level thresholds are LOW below 30, MEDIUM from 30, and HIGH from 60. Severity is an estimate based on detection and risk; the API explains that field damage was not measured. Weather data is omitted when unavailable.

Chemical management entries avoid dosage advice and tell farmers to follow locally registered product labels and consult agricultural officers. Content should be reviewed by local agronomists before field use.

## Data and operations

- SQLite database: `cropguard.db` (ignored by Git).
- Uploaded images: `uploads/` using generated filenames (ignored by Git).
- Back up the database and uploads together if preserving scan history matters.
- No demo farmer, expert, or admin password is provided. Register a farmer through the API; provision elevated roles securely out of band.
- Tests can be run with `pytest` after installing `requirements.txt`.

## Known limitations

- Farmer and authorized expert image preview routes and scan PDF reports are implemented; these require the configured storage adapter and the ReportLab dependency respectively.
- Apply all checked-in migrations, including `202609270002_public_schema_role_grants.sql`, with the Supabase CLI (`supabase link`, then `supabase db push`). Set `DATABASE_URL` to the project's PostgreSQL connection URI, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and the server-only `SUPABASE_SECRET_KEY`; configure redirect URLs and email confirmation in Supabase Auth. The migrations create the private `crop-images` bucket, RLS policies, and the grants required by the backend's authenticated SQL role.
- Copy old SQLite records after migration using `python scripts/migrate_sqlite_to_supabase.py` with `DATABASE_URL` set to the destination and optional `SQLITE_SOURCE`. User password hashes are intentionally excluded; each legacy user needs Supabase Auth signup/email verification with the same email and a new password. Existing local image files are not automatically copied into Storage.
- To exercise live RLS checks, apply the migration, create two disposable Supabase Auth farmer accounts, set `SUPABASE_TEST_DSN`, `RLS_TEST_FARMER_A_UID`, and `RLS_TEST_FARMER_B_UID`, install requirements, then run `python -m unittest tests.test_supabase_rls -v`. The integration test is skipped unless all three variables are set.
- PostgreSQL/Supabase mode, RLS behavior, private Storage, auth refresh, and PDF output require a real configured Supabase project and have not been live-verified in this environment. Do not deploy until those are configured and tested.
- Rate limiting and weather caching are in-process and reset on restart; they are suitable for a single application process, not coordinated multi-instance production deployments. External email/SMS notifications and full Marathi/Tamil translations are not configured; the localization foundation currently translates selected shared navigation and landing headings only.

### Supabase production setup

1. Install the Supabase CLI, create a project, and run `supabase link` followed by `supabase db push` from this repository.
2. In Supabase Auth, set the site URL and allowed redirect URLs for the deployed CropGuard origin. Enable email confirmation and configure SMTP if confirmation/reset mail is required.
3. Set the server environment variables from `.env.example`: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DATABASE_URL` (PostgreSQL URI). Keep all database and secret keys out of browser code and deployment logs. Set `ALLOWED_ORIGINS` to the exact deployed origin.
4. Run the one-time SQLite copy script described above. Imported password hashes are discarded; users set new passwords through Supabase Auth. To promote an account, use a controlled SQL session to set `public.users.role` to `EXPERT` or `ADMIN` for a verified account; never accept role from signup metadata.
5. Check the private `crop-images` bucket and its policies in Storage. The migration creates it; do not change its public flag. Run the live RLS test and manually verify signup, refresh, scans, private images, and expert review before launch.

Production command (configure the platform to provide `PORT` and environment variables):

```powershell
uvicorn cropguard.main:app --host 0.0.0.0 --port $env:PORT
```
- The weather provider currently returns current conditions only; no forecast is fabricated.
- No actual pest model or external credentials are included. Roboflow/weather behavior needs those values in the local `.env`.
- A read-only audit found credential-like values in two historical versions of `main.py`; the current source and `.env.example` contain none. The actual values were not printed. Revoke/rotate the Roboflow and weather credentials before using them again. This workspace cannot write `.git`, so I could not create the requested backup branch or rewrite history. In a fresh clone with coordinated collaborators, use `git-filter-repo`'s `--sensitive-data-removal --replace-text` workflow to replace each exposed value across all refs, inspect the rewritten history, then coordinate any force-push and cleanup of other clones. Do not paste those values into logs or commit the replacement file. See the [git-filter-repo sensitive data removal guide](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt) and [GitHub's sensitive data guidance](https://docs.github.com/enterprise-cloud%40latest/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

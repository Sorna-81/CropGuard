"""Core API regression tests; run with pytest or python -m unittest."""
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from cropguard import config, database
from cropguard.main import app, assess_risk, weather_for, _weather_cache


class CropGuardAPITests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path.cwd() / ".test-cropguard.db"
        self.db_path.unlink(missing_ok=True)
        self.orig_db_url = getattr(config, "DATABASE_URL", "")
        self.orig_db_database_url = getattr(database, "DATABASE_URL", "")
        self.orig_supa_enabled = getattr(config, "SUPABASE_DATABASE_ENABLED", False)
        config.DATABASE_URL = f"sqlite:///{self.db_path.as_posix()}"
        database.DATABASE_URL = config.DATABASE_URL
        config.SUPABASE_DATABASE_ENABLED = False
        database.DB_PATH = self.db_path
        database.initialize_database()
        config.JWT_SECRET_KEY = "test-only-signing-secret"
        config.ROBOFLOW_API_KEY = ""
        config.ROBOFLOW_MODEL_ID = ""
        config.ROBOFLOW_MODEL_VERSION = ""
        config.ROBOFLOW_PEST_MODEL_ID = ""
        config.ROBOFLOW_PEST_MODEL_VERSION = ""
        config.WEATHER_API_KEY = ""
        config.ENABLE_OPEN_METEO = False
        config.ENABLE_LOCAL_PREDICTOR = False
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.db_path.unlink(missing_ok=True)
        config.DATABASE_URL = self.orig_db_url
        database.DATABASE_URL = self.orig_db_database_url
        config.SUPABASE_DATABASE_ENABLED = self.orig_supa_enabled

    def farmer(self, email="farmer@example.test"):
        response = self.client.post("/api/auth/register", data={"email": email, "password": "strong-password-123", "full_name": "Test Farmer"})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_auth_profile_and_crop_crud(self):
        headers = self.farmer()
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).json()["role"], "FARMER")
        login = self.client.post("/api/auth/login", data={"email": "farmer@example.test", "password": "strong-password-123"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.put("/api/profile", headers=headers, json={"state": "Maharashtra", "district": "Pune"}).status_code, 200)
        crop = self.client.post("/api/crops", headers=headers, json={"name": "Tomato", "growth_stage": "flowering"})
        self.assertEqual(crop.status_code, 200)
        crop_id = crop.json()["id"]
        self.assertEqual(len(self.client.get("/api/crops", headers=headers).json()), 1)
        self.assertEqual(self.client.put(f"/api/crops/{crop_id}", headers=headers, json={"growth_stage": "fruiting"}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/crops/{crop_id}", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)

    def test_create_crop_with_frontend_sample_and_reload(self):
        headers = self.farmer("crop-sample@example.test")
        payload = {
            "name": "Tomato", "variety": "Hybrid", "sowing_date": "2026-09-01",
            "growth_stage": "Vegetative", "area": 2, "location": "Tirunelveli",
            "soil_type": "Red", "irrigation_type": "Drip",
        }
        created = self.client.post("/api/crops", headers=headers, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["name"], "Tomato")
        self.assertEqual(created.json()["area"], 2)
        listed = self.client.get("/api/crops", headers=headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]["variety"], "Hybrid")
        self.assertEqual(listed.json()[0]["sowing_date"], "2026-09-01")

    def test_prediction_upload_validation_and_no_fake_ai(self):
        self.assertEqual(self.client.post("/api/predict", files={"image": ("bad.txt", b"not-image", "text/plain")}).status_code, 415)
        self.assertEqual(self.client.post("/api/predict", files={"image": ("bad.jpg", b"not-image", "image/jpeg")}).status_code, 400)
        self.assertEqual(self.client.post("/api/predict", files={"image": ("large.jpg", b"0" * (5 * 1024 * 1024 + 1), "image/jpeg")}).status_code, 413)
        image = BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="JPEG")
        result = self.client.post("/api/predict", data={"detection_type": "pest"}, files={"image": ("leaf.jpg", image.getvalue(), "image/jpeg")})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "unavailable")
        self.assertIn("not configured", result.json()["message"])
        no_model = self.client.post("/api/predict", data={"detection_type": "disease"}, files={"image": ("leaf.jpg", image.getvalue(), "image/jpeg")})
        self.assertEqual(no_model.json()["status"], "unavailable")
        self.assertNotIn("prediction_id", no_model.json())

    def test_configured_ai_provider_saves_normalized_scan_and_authorized_image(self):
        headers = self.farmer()
        crop = self.client.post("/api/crops", headers=headers, json={"name": "Tomato", "location": "Pune"}).json()
        config.ROBOFLOW_API_KEY = "test-provider-key"
        config.ROBOFLOW_MODEL_ID = "cropguard-disease"
        config.ROBOFLOW_MODEL_VERSION = "3"
        image = BytesIO(); Image.new("RGB", (8, 8)).save(image, format="JPEG")
        response = Mock(); response.raise_for_status.return_value = None
        response.json.return_value = {"predictions": [{"class": "Early Blight", "confidence": 0.87}]}
        with patch("cropguard.services.prediction.requests.post", return_value=response):
            result = self.client.post("/api/predict", headers=headers, data={"crop_id": crop["id"]}, files={"image": ("leaf.jpg", image.getvalue(), "image/jpeg")})
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertEqual(payload["disease_name"], "Early Blight")
        self.assertEqual(payload["model_name"], "roboflow")
        scan = self.client.get(f"/api/scans/{payload['prediction_id']}", headers=headers).json()
        self.addCleanup(lambda: (config.UPLOAD_DIR / scan["filename"]).unlink(missing_ok=True))
        self.assertEqual(scan["model_version"], "3")
        image_response = self.client.get(f"/api/scans/{payload['prediction_id']}/image", headers=headers)
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.content, image.getvalue())

    def test_weather_fallback_and_explainable_risk(self):
        result = self.client.get("/api/weather", params={"location": "Pune"})
        self.assertFalse(result.json()["available"])
        score, level, reasons = assess_risk("Early Blight", 0.9, {"humidity": 90, "rainfall": 2, "temperature": 24})
        self.assertEqual(level, "HIGH")
        self.assertGreater(score, 0)
        self.assertTrue(any("humidity" in reason.lower() for reason in reasons))

    def test_weather_real_response_cache_and_timeout_fallback(self):
        config.WEATHER_API_KEY = "test-weather-key"
        _weather_cache.clear()
        successful = Mock(); successful.status_code = 200; successful.raise_for_status.return_value = None
        successful.json.return_value = {"main": {"temp": 22.4, "humidity": 78}, "weather": [{"description": "cloudy"}], "wind": {"speed": 2}}
        with patch("cropguard.main.requests.get", return_value=successful) as lookup:
            first = weather_for("Test Field")
            second = weather_for("test field")
        self.assertEqual(first["temperature"], 22.4)
        self.assertEqual(first, second)
        self.assertEqual(lookup.call_count, 1)
        _weather_cache.clear()
        import requests
        with patch("cropguard.main.requests.get", side_effect=requests.Timeout):
            self.assertIsNone(weather_for("Unavailable Field"))

    def test_role_authorization_and_health(self):
        headers = self.farmer()
        self.assertEqual(self.client.get("/api/admin/dashboard", headers=headers).status_code, 403)
        health = self.client.get("/health").json()
        self.assertEqual(health["database"], "connected")
        self.assertNotIn("api_key", str(health).lower())

    def test_invalid_login_and_farmer_ownership(self):
        owner_headers = self.farmer()
        other_headers = self.farmer("other@example.test")
        self.assertEqual(self.client.post("/api/auth/login", data={"email": "farmer@example.test", "password": "wrong-password"}).status_code, 401)
        crop = self.client.post("/api/crops", headers=owner_headers, json={"name": "Tomato"}).json()
        owner_id = self.client.get("/api/auth/me", headers=owner_headers).json()["id"]
        with database.connection() as db:
            db.execute("INSERT INTO crop_scans(user_id,crop_id,kind,name,created_at) VALUES(?,?,?,?,?)", (owner_id, crop["id"], "disease", "Early Blight", "2026-01-01"))
            scan_id = db.execute("SELECT max(id) FROM crop_scans").fetchone()[0]
        self.assertEqual(self.client.get(f"/api/crops/{crop['id']}", headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/scans/{scan_id}", headers=other_headers).status_code, 404)
        self.assertEqual(self.client.post(f"/api/scans/{scan_id}/expert-review", headers=other_headers, json={}).status_code, 404)

    def test_expert_cannot_read_or_submit_case_assigned_to_another_expert(self):
        farmer_headers = self.farmer()
        first = self.client.post("/api/auth/register", data={"email": "expert-one@example.test", "password": "strong-password-123"}).json()
        second = self.client.post("/api/auth/register", data={"email": "expert-two@example.test", "password": "strong-password-123"}).json()
        with database.connection() as db:
            db.execute("UPDATE users SET role='EXPERT' WHERE email IN (?,?)", ("expert-one@example.test", "expert-two@example.test"))
            farmer_id = self.client.get("/api/auth/me", headers=farmer_headers).json()["id"]
            crop = db.execute("INSERT INTO crops(user_id,name,created_at) VALUES(?,?,?)", (farmer_id, "Tomato", "now"))
            crop_id = crop.lastrowid
            scan = db.execute("INSERT INTO crop_scans(user_id,crop_id,kind,name,created_at) VALUES(?,?,?,?,?)", (farmer_id, crop_id, "disease", "Early Blight", "now"))
            case = db.execute("INSERT INTO expert_reviews(scan_id,farmer_id,expert_id,status,created_at) VALUES(?,?,?,'assigned',?)", (scan.lastrowid, farmer_id, second["user"]["id"], "now"))
            case_id = case.lastrowid
        expert_one = {"Authorization": "Bearer " + first["access_token"]}
        self.assertEqual(self.client.get(f"/api/expert/cases/{case_id}", headers=expert_one).status_code, 404)
        self.assertEqual(self.client.post(f"/api/expert/cases/{case_id}/review", headers=expert_one, json={"response": "Review"}).status_code, 404)

    def test_alerts_expert_review_and_admin_knowledge(self):
        headers = self.farmer()
        user_id = self.client.get("/api/auth/me", headers=headers).json()["id"]
        crop = self.client.post("/api/crops", headers=headers, json={"name": "Tomato"}).json()
        with database.connection() as db:
            db.execute("INSERT INTO crop_scans(user_id,crop_id,kind,name,created_at) VALUES(?,?,?,?,?)", (user_id, crop["id"], "disease", "Early Blight", "now"))
            scan_id = db.execute("SELECT max(id) FROM crop_scans").fetchone()[0]
            db.execute("INSERT INTO expert_reviews(scan_id,farmer_id,created_at) VALUES(?,?,?)", (scan_id, user_id, "now"))
            case_id = db.execute("SELECT max(id) FROM expert_reviews").fetchone()[0]
            db.execute("INSERT INTO alerts(user_id,message,created_at) VALUES(?,?,?)", (user_id, "Risk alert", "now"))
            alert_id = db.execute("SELECT max(id) FROM alerts").fetchone()[0]
            db.execute("UPDATE users SET role='EXPERT' WHERE id=?", (user_id,))
        self.assertEqual(self.client.post(f"/api/expert/cases/{case_id}/review", headers=headers, json={"response": "Please consult your local extension officer."}).status_code, 200)
        self.assertEqual(self.client.put(f"/api/alerts/{alert_id}/read", headers=headers).status_code, 200)
        with database.connection() as db:
            db.execute("UPDATE users SET role='ADMIN' WHERE id=?", (user_id,))
        admin_entry = self.client.post("/api/admin/diseases", headers=headers, json={"name": "Test disease", "symptoms": ["test symptom"]})
        self.assertEqual(admin_entry.status_code, 200, admin_entry.text)
        self.assertEqual(self.client.put(f"/api/admin/diseases/{admin_entry.json()['id']}", headers=headers, json={"scientific_name": "Test species"}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/admin/diseases/{admin_entry.json()['id']}", headers=headers).status_code, 200)

    def test_local_prediction_provider(self):
        headers = self.farmer()
        crop = self.client.post("/api/crops", headers=headers, json={"name": "Tomato", "location": "Pune"}).json()
        config.ENABLE_LOCAL_PREDICTOR = True
        image = BytesIO()
        Image.new("RGB", (64, 64), color="green").save(image, format="JPEG")
        result = self.client.post("/api/predict", headers=headers, data={"crop_id": crop["id"]}, files={"image": ("leaf.jpg", image.getvalue(), "image/jpeg")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "success")
        self.assertIn("prediction_id", result.json())
        self.assertEqual(result.json()["model_name"], "cropguard-vision-engine")

    def test_health_reflects_actual_configuration(self):
        config.ENABLE_LOCAL_PREDICTOR = True
        config.WEATHER_API_KEY = ""
        health_unconfigured = self.client.get("/health").json()
        self.assertEqual(health_unconfigured["database"], "connected")
        self.assertEqual(health_unconfigured["weather_service"], "not_configured")
        self.assertEqual(health_unconfigured["disease_ai"], "configured")
        self.assertEqual(health_unconfigured["pest_ai"], "configured")
        self.assertEqual(health_unconfigured["status"], "healthy")

        config.WEATHER_API_KEY = "some-active-key"
        health_configured = self.client.get("/health").json()
        self.assertEqual(health_configured["weather_service"], "configured")

    def test_weather_endpoints_unconfigured_and_configured(self):
        config.WEATHER_API_KEY = ""
        res_unconf = self.client.get("/api/weather", params={"location": "Tirunelveli"}).json()
        self.assertFalse(res_unconf["available"])
        self.assertIn("not configured", res_unconf["message"].lower())

        test_w_unconf = self.client.get("/test-weather", params={"location": "Tirunelveli"}).json()
        self.assertEqual(test_w_unconf["status"], "not_configured")

        config.WEATHER_API_KEY = "test-weather-key"
        _weather_cache.clear()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "name": "Tirunelveli",
            "main": {"temp": 31.5, "humidity": 82},
            "weather": [{"description": "scattered clouds"}],
            "wind": {"speed": 4.1},
            "rain": {"1h": 1.2},
        }
        with patch("cropguard.main.requests.get", return_value=mock_resp):
            res_conf = self.client.get("/api/weather", params={"location": "Tirunelveli"}).json()
            self.assertTrue(res_conf["available"])
            data = res_conf["weather_data"]
            self.assertEqual(data["location"], "Tirunelveli")
            self.assertEqual(data["temperature"], 31.5)
            self.assertEqual(data["humidity"], 82)
            self.assertEqual(data["wind_speed"], 4.1)
            self.assertEqual(data["rainfall"], 1.2)
            self.assertEqual(data["conditions"], "Scattered clouds")

            test_w_conf = self.client.get("/test-weather", params={"location": "Tirunelveli"}).json()
            self.assertEqual(test_w_conf["status"], "success")

    def test_weather_open_meteo_fallback(self):
        config.WEATHER_API_KEY = ""
        config.ENABLE_OPEN_METEO = True
        _weather_cache.clear()

        def mock_get(url, *args, **kwargs):
            resp = Mock()
            resp.status_code = 200
            if "geocoding-api.open-meteo.com" in url:
                resp.json.return_value = {
                    "results": [
                        {"name": "Tirunelveli", "latitude": 8.7274, "longitude": 77.6838}
                    ]
                }
            elif "api.open-meteo.com" in url:
                resp.json.return_value = {
                    "current": {
                        "temperature_2m": 32.2,
                        "relative_humidity_2m": 55,
                        "precipitation": 0.0,
                        "weather_code": 1,
                        "wind_speed_10m": 4.5,
                    }
                }
            else:
                resp.status_code = 404
            return resp

        with patch("cropguard.main.requests.get", side_effect=mock_get):
            res = self.client.get("/api/weather", params={"location": "Tirunelveli"}).json()
            self.assertTrue(res["available"])
            data = res["weather_data"]
            self.assertEqual(data["location"], "Tirunelveli")
            self.assertEqual(data["temperature"], 32.2)
            self.assertEqual(data["humidity"], 55)
            self.assertEqual(data["conditions"], "Mainly clear")
            self.assertEqual(data["wind_speed"], 4.5)
            self.assertEqual(data["rainfall"], 0.0)

            test_w = self.client.get("/test-weather", params={"location": "Tirunelveli"}).json()
            self.assertEqual(test_w["status"], "success")

    def test_complete_scan_flow_with_tomato_leaf_weather_fallback_and_available(self):
        headers = self.farmer()
        crop = self.client.post("/api/crops", headers=headers, json={"name": "Tomato", "location": "Tirunelveli"}).json()
        config.ENABLE_LOCAL_PREDICTOR = True

        # Create a synthetic leaf image with brown spots to trigger Early Blight detection
        leaf_img = Image.new("RGB", (100, 100), color=(34, 139, 34))  # green base
        # Add brown/necrotic spot
        for x in range(30, 60):
            for y in range(30, 60):
                leaf_img.putpixel((x, y), (139, 69, 19))
        img_buf = BytesIO()
        leaf_img.save(img_buf, format="JPEG")
        image_bytes = img_buf.getvalue()

        # 1. Scan with weather UNAVAILABLE (empty key)
        config.WEATHER_API_KEY = ""
        scan_no_weather = self.client.post(
            "/api/scans",
            headers=headers,
            data={"crop_id": crop["id"], "cropType": "Tomato", "location": "Tirunelveli", "detection_type": "disease"},
            files={"image": ("tomato_leaf.jpg", image_bytes, "image/jpeg")},
        )
        self.assertEqual(scan_no_weather.status_code, 200, scan_no_weather.text)
        payload_no_w = scan_no_weather.json()
        self.assertEqual(payload_no_w["status"], "success")
        self.assertEqual(payload_no_w["disease_name"], "Early Blight")
        self.assertFalse(payload_no_w["weather_available"])
        self.assertIsNone(payload_no_w["weather"])
        self.assertTrue(any("weather" in r.lower() and "unavailable" in r.lower() for r in payload_no_w["risk_reasons"]))

        # Verify saved in scan history and PDF report can be generated
        scan_id_1 = payload_no_w["prediction_id"]
        detail_1 = self.client.get(f"/api/scans/{scan_id_1}", headers=headers).json()
        self.assertEqual(detail_1["name"], "Early Blight")
        pdf_1 = self.client.get(f"/api/scans/{scan_id_1}/report.pdf", headers=headers)
        self.assertEqual(pdf_1.status_code, 200)
        self.assertEqual(pdf_1.headers["content-type"], "application/pdf")

        # 2. Scan with weather CONFIGURED and working
        config.WEATHER_API_KEY = "test-weather-key"
        _weather_cache.clear()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "name": "Tirunelveli",
            "main": {"temp": 28.0, "humidity": 85},
            "weather": [{"description": "light rain"}],
            "wind": {"speed": 3.0},
            "rain": {"1h": 2.5},
        }
        with patch("cropguard.main.requests.get", return_value=mock_resp):
            scan_with_weather = self.client.post(
                "/api/scans",
                headers=headers,
                data={"crop_id": crop["id"], "cropType": "Tomato", "location": "Tirunelveli", "detection_type": "disease"},
                files={"image": ("tomato_leaf.jpg", image_bytes, "image/jpeg")},
            )
        self.assertEqual(scan_with_weather.status_code, 200, scan_with_weather.text)
        payload_w = scan_with_weather.json()
        self.assertEqual(payload_w["status"], "success")
        self.assertEqual(payload_w["disease_name"], "Early Blight")
        self.assertTrue(payload_w["weather_available"])
        self.assertIsNotNone(payload_w["weather"])
        self.assertEqual(payload_w["weather"]["location"], "Tirunelveli")
        self.assertEqual(payload_w["weather"]["humidity"], 85)
        # Verify weather factors influenced risk
        self.assertTrue(any("humidity" in r.lower() for r in payload_w["risk_reasons"]))
        self.assertTrue(any("rainfall" in r.lower() for r in payload_w["risk_reasons"]))

        # Verify saved in scan history and PDF report
        scan_id_2 = payload_w["prediction_id"]
        detail_2 = self.client.get(f"/api/scans/{scan_id_2}", headers=headers).json()
        self.assertEqual(detail_2["name"], "Early Blight")
        self.assertIsInstance(detail_2["weather"], dict)
        self.assertEqual(detail_2["weather"]["location"], "Tirunelveli")

        pdf_2 = self.client.get(f"/api/scans/{scan_id_2}/report.pdf", headers=headers)
        self.assertEqual(pdf_2.status_code, 200)
        self.assertEqual(pdf_2.headers["content-type"], "application/pdf")

    def test_scan_persistence_ownership_and_history_retrieval(self):
        """Verify scan persistence to DB, history loading, and user ownership isolation."""
        farmer_a = self.farmer("farmer_a@test.org")
        farmer_b = self.farmer("farmer_b@test.org")
        config.ENABLE_LOCAL_PREDICTOR = True

        crop_a = self.client.post("/api/crops", headers=farmer_a, json={"name": "Tomato", "location": "Madurai"}).json()

        # Farmer B initially has empty history and empty alerts
        empty_history_b = self.client.get("/api/history", headers=farmer_b).json()
        self.assertEqual(len(empty_history_b), 0)
        empty_alerts_b = self.client.get("/api/alerts", headers=farmer_b).json()
        self.assertEqual(len(empty_alerts_b), 0)

        # Farmer A performs a scan
        leaf_img = Image.new("RGB", (100, 100), color=(34, 139, 34))
        for x in range(30, 60):
            for y in range(30, 60):
                leaf_img.putpixel((x, y), (139, 69, 19))
        img_buf = BytesIO()
        leaf_img.save(img_buf, format="JPEG")

        scan_res = self.client.post(
            "/api/scans",
            headers=farmer_a,
            data={"crop_id": crop_a["id"], "cropType": "Tomato", "location": "Madurai", "detection_type": "disease"},
            files={"image": ("tomato.jpg", img_buf.getvalue(), "image/jpeg")},
        )
        self.assertEqual(scan_res.status_code, 200)
        scan_data = scan_res.json()
        self.assertEqual(scan_data["status"], "success")
        self.assertEqual(scan_data["disease_name"], "Early Blight")
        scan_id = scan_data["prediction_id"]

        # Farmer A retrieves history
        history_a = self.client.get("/api/history", headers=farmer_a).json()
        self.assertGreaterEqual(len(history_a), 1)
        first_scan = next(s for s in history_a if s["id"] == scan_id)
        self.assertEqual(first_scan["name"], "Early Blight")
        self.assertEqual(first_scan["crop_id"], crop_a["id"])
        self.assertEqual(first_scan["kind"], "disease")
        self.assertIn("risk_level", first_scan)
        self.assertIn("confidence", first_scan)

        # Farmer B MUST NOT see Farmer A's scan (strict tenant isolation)
        history_b = self.client.get("/api/history", headers=farmer_b).json()
        self.assertEqual(len(history_b), 0)

    def test_high_risk_alert_creation_and_no_alert_for_medium_risk(self):
        """Verify alerts are created ONLY when risk is HIGH and properly joined with crop/scan details."""
        farmer = self.farmer("farmer_alert@test.org")
        config.ENABLE_LOCAL_PREDICTOR = True
        crop = self.client.post("/api/crops", headers=farmer, json={"name": "Tomato", "location": "Coimbatore"}).json()

        leaf_img = Image.new("RGB", (100, 100), color=(34, 139, 34))
        for x in range(30, 60):
            for y in range(30, 60):
                leaf_img.putpixel((x, y), (139, 69, 19))
        img_buf = BytesIO()
        leaf_img.save(img_buf, format="JPEG")
        image_bytes = img_buf.getvalue()

        # 1. Medium risk scan (no weather / moderate weather) -> score = 30 -> MEDIUM -> NO alert
        config.WEATHER_API_KEY = ""
        scan_medium = self.client.post(
            "/api/scans",
            headers=farmer,
            data={"crop_id": crop["id"], "cropType": "Tomato", "location": "Coimbatore", "detection_type": "disease"},
            files={"image": ("leaf.jpg", image_bytes, "image/jpeg")},
        ).json()
        self.assertEqual(scan_medium["risk_level"].upper(), "MEDIUM")
        alerts_1 = self.client.get("/api/alerts", headers=farmer).json()
        self.assertEqual(len(alerts_1), 0)

        # 2. High risk scan (weather with 85% humidity + rain) -> score >= 60 -> HIGH -> ALERT created
        config.WEATHER_API_KEY = "test-key"
        _weather_cache.clear()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "name": "Coimbatore",
            "main": {"temp": 27.5, "humidity": 88},
            "weather": [{"description": "heavy rain"}],
            "wind": {"speed": 4.5},
            "rain": {"1h": 5.0},
        }
        with patch("cropguard.main.requests.get", return_value=mock_resp):
            scan_high = self.client.post(
                "/api/scans",
                headers=farmer,
                data={"crop_id": crop["id"], "cropType": "Tomato", "location": "Coimbatore", "detection_type": "disease"},
                files={"image": ("leaf.jpg", image_bytes, "image/jpeg")},
            ).json()

        self.assertEqual(scan_high["risk_level"].upper(), "HIGH")
        alerts_2 = self.client.get("/api/alerts", headers=farmer).json()
        self.assertEqual(len(alerts_2), 1)
        alert = alerts_2[0]
        self.assertEqual(alert["crop_id"], crop["id"])
        self.assertEqual(alert["crop_name"], "Tomato")
        self.assertEqual(alert["disease_name"], "Early Blight")
        self.assertEqual(alert["severity"], "HIGH")
        self.assertEqual(alert["is_read"], 0)

        # Mark alert as read
        read_res = self.client.put(f"/api/alerts/{alert['id']}/read", headers=farmer)
        self.assertEqual(read_res.status_code, 200)
        alerts_3 = self.client.get("/api/alerts", headers=farmer).json()
        self.assertEqual(alerts_3[0]["is_read"], 1)

    def test_weather_failure_does_not_break_scan(self):
        """Verify OpenWeatherMap API failure gracefully falls back without breaking prediction or persistence."""
        farmer = self.farmer("farmer_weather_err@test.org")
        config.ENABLE_LOCAL_PREDICTOR = True
        config.WEATHER_API_KEY = "test-key"
        _weather_cache.clear()
        crop = self.client.post("/api/crops", headers=farmer, json={"name": "Tomato", "location": "Salem"}).json()

        leaf_img = Image.new("RGB", (100, 100), color=(34, 139, 34))
        for x in range(30, 60):
            for y in range(30, 60):
                leaf_img.putpixel((x, y), (139, 69, 19))
        img_buf = BytesIO()
        leaf_img.save(img_buf, format="JPEG")

        import requests
        with patch("cropguard.main.requests.get", side_effect=requests.ConnectionError("Weather service down")):
            res = self.client.post(
                "/api/scans",
                headers=farmer,
                data={"crop_id": crop["id"], "cropType": "Tomato", "location": "Salem", "detection_type": "disease"},
                files={"image": ("leaf.jpg", img_buf.getvalue(), "image/jpeg")},
            )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["disease_name"], "Early Blight")
        self.assertFalse(data["weather_available"])
        self.assertIn("weather", data["weather_message"].lower())

        # Verify scan was still saved to history despite weather failure
        scan_id = data["prediction_id"]
        detail = self.client.get(f"/api/scans/{scan_id}", headers=farmer)
        self.assertEqual(detail.status_code, 200)

    def test_frontend_language_selector_removed_and_empty_states_present(self):
        """Verify UI contracts: English only, no languageSwitcher, professional empty states."""
        html_file = config.STATIC_DIR / "index.html"
        self.assertTrue(html_file.exists())
        html_content = html_file.read_text(encoding="utf-8")

        # Navbar language selector removed
        self.assertNotIn('id="languageSwitcher"', html_content)
        self.assertNotIn('class="language-selector"', html_content)

        # Empty states exist with professional copy
        self.assertIn('id="historyEmpty"', html_content)
        self.assertIn("Your crop scans will appear here after you complete your first scan.", html_content)
        self.assertIn('id="alertsEmpty"', html_content)
        self.assertIn("No active alerts", html_content)
        self.assertIn("High-risk crop conditions detected during your scans will appear here.", html_content)

        # JS contracts
        app_js = (config.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("riskClass", app_js)
        self.assertNotIn("$('#languageSwitcher')", app_js)


if __name__ == "__main__":
    unittest.main()



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
        database.DB_PATH = self.db_path
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


if __name__ == "__main__":
    unittest.main()

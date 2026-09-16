"""
tests/test_auth_protection.py
-----------------------------
Regression tests for the login-protection layer added to app.py:

  - token + pin gating on admin mutation routes (401 no token / 403 wrong pin)
  - /login/creds success path (httpOnly cookie + X-Set-Auth-Token header)
  - per-IP rate limiting (5 failed attempts -> 1-hour lockout, 429)
  - /admin/api/clusters JSON polling endpoint
  - /admin/authorities/add registration
  - /upload tokenized flow (no token -> hard redirect to /login)
  - exact-origin CORS headers

Run:  python -m unittest tests.test_auth_protection -v
Safety: EMAIL_TEST_MODE forced true before `app` is imported, and a temp
DB path is installed before import so no real data/email is touched.
"""

import os
import sys
import tempfile
import unittest

import db
import email_driver

# ---- install isolated, safe environment BEFORE app imports -----------------
_TMP = tempfile.mkdtemp(prefix="ss_auth_")
os.environ["EMAIL_TEST_MODE"] = "true"     # never a real send
os.environ["ADMIN_PIN"] = "smart2026"
os.environ["RESEND_API_KEY"] = ""
os.environ["RESEND_FROM_EMAIL"] = ""
os.environ["CORS_ORIGINS"] = "http://localhost:5000,http://127.0.0.1:5000"

db.DB_PATH = os.path.join(_TMP, "test.db")

import app  # noqa: E402  (after env/db isolation)


class AuthProtectionTest(unittest.TestCase):

    def setUp(self):
        app._rate_store.clear()   # per-IP limiter is process-global; isolate tests
        self.client = app.app.test_client()

    # ------------------------------------------------------------------
    # token + pin gating
    # ------------------------------------------------------------------
    def test_locked_routes_return_401_without_token(self):
        for url in ("/admin/approve/1", "/admin/reject/1", "/admin/test-email"):
            r = self.client.post(url, data={"pin": "smart2026"})
            self.assertEqual(r.status_code, 401, url)
            self.assertEqual(r.get_json().get("reason"), "blocked")

    def test_locked_routes_return_403_without_pin(self):
        r = self.client.post("/login/creds", data={"pin": "smart2026"})
        token = r.headers.get("X-Set-Auth-Token")
        self.assertTrue(token)
        r = self.client.post("/admin/approve/1",
                             data={"pin": "wrongpin"},
                             headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json().get("reason"), "blocked")

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------
    def test_login_success_issues_cookie_and_header_token(self):
        r = self.client.post("/login/creds", data={"pin": "smart2026"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("ok"))
        self.assertTrue(r.headers.get("X-Set-Auth-Token"))
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any("ss_token=" in c and "HttpOnly" in c for c in cookies),
                        "token must be set as an httpOnly cookie")

    def test_login_wrong_pin_fails_soft(self):
        r = self.client.post("/login/creds", data={"pin": "nope"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.get_json().get("ok"))

    def test_auth_token_endpoint_issues_token(self):
        r = self.client.post("/auth/token", data={"pin": "smart2026"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("X-Set-Auth-Token"))

    # ------------------------------------------------------------------
    # rate limiting (per IP)
    # ------------------------------------------------------------------
    def test_rate_limit_locks_after_5_failures(self):
        client = app.app.test_client()
        for _ in range(5):
            client.post("/login/creds", data={"pin": "bad"})
        r = client.post("/login/creds", data={"pin": "bad"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json().get("reason"), "rate_limited")

    # ------------------------------------------------------------------
    # admin API / authorities / upload token
    # ------------------------------------------------------------------
    def test_admin_clusters_api_returns_json(self):
        r = self.client.get("/admin/api/clusters")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("clusters", body)
        self.assertIn("authorities", body)

    def test_authority_add_requires_auth_then_registers(self):
        r = self.client.post("/admin/authorities/add", data={
            "authority_area_key": "22.57:88.36",
            "hazard_type": "road_damage",
            "email": "committee@example.org",
            "pin": "smart2026",
        })
        self.assertEqual(r.status_code, 401)

        # login then register
        login = self.client.post("/login/creds", data={"pin": "smart2026"})
        token = login.headers.get("X-Set-Auth-Token")
        r = self.client.post("/admin/authorities/add", data={
            "authority_area_key": "22.57:88.36",
            "hazard_type": "road_damage",
            "email": "committee@example.org",
            "pin": "smart2026",
        }, headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["lifecycle"], "pending")

        rows = app.authority_routing.list_authorities_for_admin()
        self.assertTrue(any(a["email"] == "committee@example.org" for a in rows))

    def test_upload_without_token_redirects_to_login(self):
        r = self.client.post("/upload", data={})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    def test_upload_with_valid_token_passes_gate(self):
        utok = self.client.get("/upload/token").get_json()["token"]
        r = self.client.post("/upload", data={"_upload_token": utok})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/login", r.headers.get("Location", ""))

    # ------------------------------------------------------------------
    # CORS exact-origin
    # ------------------------------------------------------------------
    def test_cors_exact_origin(self):
        r = self.client.get("/admin/api/clusters",
                            headers={"Origin": "http://localhost:5000"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"),
                         "http://localhost:5000")

    def test_cors_rejects_unknown_origin(self):
        r = self.client.get("/admin/api/clusters",
                            headers={"Origin": "https://evil.example.org"})
        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    # ------------------------------------------------------------------
    # test-email endpoint honors auth + test mode
    # ------------------------------------------------------------------
    def test_test_email_endpoint_auth_and_mode(self):
        # give the test a seeded approved detection so /admin/test-email succeeds
        letter_path = os.path.join(_TMP, "approved_letter.pdf")
        with open(letter_path, "wb") as fh:
            fh.write(b"%PDF-1.4 test-kb")
        db.insert_detection(
            image_path=os.path.join(_TMP, "nope.jpg"),
            source="citizen", damage_class="pothole", confidence=0.9,
            severity="Warning", lat=12.9716, lon=77.5946,
            ai_accepted=1, client_ip="192.0.2.99",
        )
        db.update_status(db.get_detection(1)["id"], "approved", letter_path=letter_path)

        # no token -> blocked
        self.assertEqual(self.client.post("/admin/test-email",
                                          data={"pin": "smart2026"}).status_code, 401)

        login = self.client.post("/login/creds", data={"pin": "smart2026"})
        token = login.headers.get("X-Set-Auth-Token")
        r = self.client.post("/admin/test-email",
                             data={"pin": "smart2026"},
                             headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "test")
        self.assertEqual(body["to"], email_driver.TEST_RECIPIENT)


if __name__ == "__main__":
    unittest.main()
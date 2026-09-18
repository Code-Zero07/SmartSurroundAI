"""
tests/test_admin_api.py
-----------------------
HTTP-level admin cluster routes: approval, send idempotency, settle, and
missing-authority-email recovery.

Run:  python -m unittest discover -s tests -p "test_admin_api.py" -v
"""

import os
import tempfile
import unittest

os.environ["EMAIL_TEST_MODE"] = "true"
os.environ["AUTH_DISABLED"] = "0"
os.environ["ADMIN_PIN"] = "smart2026"

_TMP = tempfile.mkdtemp(prefix="ss_adminapi_")
os.environ["RESEND_API_KEY"] = ""
os.environ["RESEND_FROM_EMAIL"] = ""

import db  # noqa: E402
db.DB_PATH = os.path.join(_TMP, "test.db")
db.init_db()

import app  # noqa: E402
import corroboration  # noqa: E402
import email_driver  # noqa: E402


class _RecordingDriver:
    def __init__(self):
        self.calls = []

    def send(self, subject, recipient, body, attachment_path):
        self.calls.append({"recipient": recipient, "subject": subject})
        return {"ok": True, "to": recipient, "mode": "recording"}


class AdminClusterApiTest(unittest.TestCase):

    def setUp(self):
        app._rate_store.clear()
        db.DB_PATH = os.path.join(_TMP, "test.db")
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()
        self.client = app.app.test_client()
        self._recorder = _RecordingDriver()
        self._orig = email_driver.get_driver
        email_driver.get_driver = lambda: self._recorder
        email_driver.TEST_MODE = True
        self.lat, self.lon = 22.5710, 88.3639
        self.fine = corroboration.fine_area_key(self.lat, self.lon)
        self.coarse = corroboration.coarse_area_key(self.lat, self.lon)

    def tearDown(self):
        email_driver.get_driver = self._orig

    def _login(self):
        login = self.client.post("/login/creds", data={"pin": "smart2026"})
        return login.headers.get("X-Set-Auth-Token")

    def _seed_cluster(self, with_authority=True):
        rid = db.insert_detection(
            image_path="x.jpg", source="citizen", damage_class="pothole",
            confidence=0.9, severity="Warning", lat=self.lat, lon=self.lon,
            ai_accepted=1, hazard_category="road_damage", hazard_type="pothole",
            corroboration_area_key=self.fine, authority_area_key=self.coarse)
        cid = corroboration.assign_report_to_cluster(
            rid, self.fine, "road_damage", authority_area_key=self.coarse)
        db.update_status(rid, "approved")
        corroboration.recompute_for_report(rid)
        if with_authority:
            auth = db.get_or_create_authority(self.coarse, "road_damage",
                                              "authority@example.gov")
            db.mark_authority_verified(auth["id"])
        return rid, cid

    def test_cluster_approve_requires_auth(self):
        rid, cid = self._seed_cluster()
        r = self.client.post(f"/admin/cluster/{cid}/approve", data={"pin": "smart2026"})
        self.assertEqual(r.status_code, 401)

    def test_cluster_approve_sends_and_marks_sent(self):
        rid, cid = self._seed_cluster(with_authority=True)
        token = self._login()
        r = self.client.post(f"/admin/cluster/{cid}/approve",
                             data={"pin": "smart2026"},
                             headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 200, r.get_json())
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "sent")
        self.assertIsNotNone(cluster["sent_at"])
        self.assertEqual(len(self._recorder.calls), 1)

    def test_cluster_missing_authority_then_recovery(self):
        rid, cid = self._seed_cluster(with_authority=False)
        token = self._login()
        r = self.client.post(f"/admin/cluster/{cid}/approve",
                             data={"pin": "smart2026"},
                             headers={"X-Auth-Token": token})
        self.assertEqual(db.get_cluster(cid)["lifecycle"],
                         "missing_authority_email")
        self.assertEqual(len(self._recorder.calls), 0)

        email_driver.TEST_MODE = False
        r = self.client.post(
            f"/admin/cluster/{cid}/authority-email",
            data={"pin": "smart2026", "email": "authority@example.gov"},
            headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(db.get_cluster(cid)["lifecycle"], "sent")
        self.assertEqual(len(self._recorder.calls), 1)
        self.assertEqual(self._recorder.calls[0]["recipient"], "authority@example.gov")

    def test_send_route_is_idempotent_at_http(self):
        rid, cid = self._seed_cluster(with_authority=True)
        token = self._login()
        self.client.post(f"/admin/cluster/{cid}/approve",
                         data={"pin": "smart2026"}, headers={"X-Auth-Token": token})
        r = self.client.post(f"/admin/cluster/{cid}/send",
                             data={"pin": "smart2026"}, headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self._recorder.calls), 1)

    def test_settle_rejects_later_actions(self):
        rid, cid = self._seed_cluster(with_authority=True)
        token = self._login()
        r = self.client.post(f"/admin/cluster/{cid}/settle",
                             data={"pin": "smart2026"}, headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_cluster(cid)["lifecycle"], "settled")
        for path in (f"/admin/cluster/{cid}/approve", f"/admin/cluster/{cid}/send"):
            r = self.client.post(path, data={"pin": "smart2026"},
                                 headers={"X-Auth-Token": token})
            self.assertEqual(r.status_code, 400)

    def test_per_report_approve_recomputes_but_never_emails(self):
        rid, cid = self._seed_cluster(with_authority=True)
        token = self._login()
        # seed the SECOND lower-confidence report and approve it via the API
        rid2 = db.insert_detection(
            image_path="y.jpg", source="citizen", damage_class="pothole",
            confidence=0.7, severity="Warning", lat=self.lat, lon=self.lon,
            ai_accepted=1, hazard_category="road_damage", hazard_type="pothole",
            corroboration_area_key=self.fine, authority_area_key=self.coarse)
        corroboration.assign_report_to_cluster(
            rid2, self.fine, "road_damage", authority_area_key=self.coarse)
        r = self.client.post(f"/admin/approve/{rid2}",
                             data={"pin": "smart2026"}, headers={"X-Auth-Token": token})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(self._recorder.calls), 0,
                         "per-report approval must never trigger email")
        self.assertEqual(db.get_detection(rid2)["status"], "approved")
        # representative is still the higher-confidence report
        self.assertEqual(db.get_cluster(cid)["representative_report_id"], rid)


if __name__ == "__main__":
    unittest.main()
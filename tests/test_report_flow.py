"""
tests/test_report_flow.py
-------------------------
Citizen API flow: token-gated, stateless detect, authoritative re-detection on
preview, draft finalized on submit, NO email anywhere on the citizen path.

Run:  python -m unittest tests.test_report_flow -v
Forces EMAIL_TEST_MODE=true and a temp DB before importing app; patches
hazard.default_service and geocode.reverse_geocode so no ML/network runs.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

os.environ["EMAIL_TEST_MODE"] = "true"
os.environ["AUTH_DISABLED"] = "0"
os.environ["ADMIN_PIN"] = "smart2026"

_TMP = tempfile.mkdtemp(prefix="ss_flow_")
os.environ["RESEND_API_KEY"] = ""
os.environ["RESEND_FROM_EMAIL"] = ""

import db  # noqa: E402
db.DB_PATH = os.path.join(_TMP, "test.db")
db.init_db()

import app  # noqa: E402
import hazard  # noqa: E402
import geocode  # noqa: E402
import cluster_service  # noqa: E402

from hazard import DetectionResult, DetectionService  # noqa: E402


class _StubModel:
    model_name = "stub"
    model_version = "v0"

    def __init__(self, result):
        self._result = result

    def detect(self, image_path):
        return self._result


class _RecordingDriver:
    def __init__(self):
        self.calls = []

    def send(self, subject, recipient, body, attachment_path):
        self.calls.append({"recipient": recipient, "subject": subject})
        return {"ok": True, "to": recipient, "mode": "recording"}


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 120, 120)).save(buf, format="PNG")
    buf.seek(0)
    return buf


def _stub_service(detected=True, category="road_damage", htype="pothole",
                  confidence=0.9, valid=True):
    svc = DetectionService()
    svc.register(category, _StubModel(DetectionResult(
        detected=detected, hazard_category=category, hazard_type=htype,
        confidence=confidence)), min_conf=0.35)
    # validate currently flows through real threshold logic; force the outcome
    orig = svc.validate
    if not valid:
        svc.validate = lambda result: False
    return svc


class ReportFlowTest(unittest.TestCase):

    def setUp(self):
        app._rate_store.clear()
        db.DB_PATH = os.path.join(_TMP, "test.db")
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()
        self.client = app.app.test_client()
        self._orig_upload_dir = app.UPLOAD_DIR
        app.UPLOAD_DIR = os.path.join(_TMP, "uploads")
        os.makedirs(app.UPLOAD_DIR, exist_ok=True)
        self._orig_default_service = hazard.default_service
        self._orig_reverse = geocode.reverse_geocode
        self._orig_get_driver = app.email_driver.get_driver
        self.recorder = _RecordingDriver()
        app.email_driver.get_driver = lambda: self.recorder

    def tearDown(self):
        hazard.default_service = self._orig_default_service
        geocode.reverse_geocode = self._orig_reverse
        app.email_driver.get_driver = self._orig_get_driver
        app.UPLOAD_DIR = self._orig_upload_dir

    def _tok(self):
        return self.client.get("/upload/token").get_json()["token"]

    def test_geocode_requires_token(self):
        r = self.client.get("/api/geocode?lat=22&lon=88")
        self.assertEqual(r.status_code, 401)

    def test_geocode_returns_labels(self):
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": "College More", "authority_area": "Sector V / Kolkata"})
        r = self.client.get(f"/api/geocode?lat=22.5710&lon=88.3639&_upload_token={self._tok()}")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["location_name"], "College More")
        self.assertEqual(body["authority_area"], "Sector V / Kolkata")

    def test_geocode_rejects_bad_coords(self):
        for q in ("lat=abc&lon=88", "lat=99&lon=88"):
            r = self.client.get(f"/api/geocode?{q}&_upload_token={self._tok()}")
            self.assertEqual(r.status_code, 400)

    def test_detect_is_stateless_and_normalized(self):
        hazard.default_service = mock.Mock(return_value=_stub_service())
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["hazard_category"], "road_damage")
        self.assertEqual(body["hazard_type"], "pothole")
        self.assertTrue(body["valid"])
        self.assertEqual(db.list_by_status("pending"), [])

    def test_detect_rejects_unsupported_category(self):
        hazard.default_service = mock.Mock(return_value=DetectionService())
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "hazard_category": "waterlogging",
                  "lat": "22.5710", "lon": "88.3639",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_preview_drafts_only_when_valid(self):
        hazard.default_service = mock.Mock(return_value=_stub_service())
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": "College More", "authority_area": "Sector V / Kolkata"})
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "Near College More",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["ok"])
        report_id = body["report_id"]
        draft = db.get_detection(report_id)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["hazard_category"], "road_damage")
        self.assertEqual(draft["hazard_type"], "pothole")
        self.assertEqual(draft["location_name"], "Near College More")
        self.assertEqual(draft["authority_area"], "Sector V / Kolkata")
        # the preview PDF is reachable
        pr = self.client.get(f"/api/report/{report_id}/pdf?_upload_token={self._tok()}")
        self.assertEqual(pr.status_code, 200)
        self.assertTrue(pr.data.startswith(b"%PDF"))

    def test_preview_invalid_detection_does_not_draft(self):
        hazard.default_service = mock.Mock(return_value=_stub_service(valid=False))
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": None, "authority_area": None})
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)

    def test_submit_finalizes_draft_no_email(self):
        hazard.default_service = mock.Mock(return_value=_stub_service())
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": "College More", "authority_area": "Sector V / Kolkata"})
        preview = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "Near College More",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data").get_json()
        report_id = preview["report_id"]

        submitted = self.client.post(
            f"/api/report/{report_id}/submit",
            data={"_upload_token": self._tok()})
        self.assertEqual(submitted.status_code, 200, submitted.get_json())
        self.assertEqual(db.get_detection(report_id)["status"], "pending")
        self.assertEqual(len(self.recorder.calls), 0,
                         "citizen submission must never send email")

    def test_submit_rejects_non_draft(self):
        r = self.client.post("/api/report/1/submit",
                             data={"_upload_token": self._tok()})
        self.assertEqual(r.status_code, 400)

    def test_upload_queues_without_email(self):
        hazard.default_service = mock.Mock(return_value=_stub_service())
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": "College More", "authority_area": "Sector V / Kolkata"})
        r = self.client.post(
            "/upload",
            data={"_upload_token": self._tok(), "source": "esp32",
                  "lat": "22.5710", "lon": "88.3639",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(db.list_by_status("pending")), 1)
        self.assertEqual(len(self.recorder.calls), 0)


if __name__ == "__main__":
    unittest.main()
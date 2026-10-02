"""
tests/test_report_flow.py
-------------------------
Citizen API flow: token-gated, stateless detect, authoritative re-detection on
preview, draft finalized on submit, NO email anywhere on the citizen path.

Run:  python -m unittest tests.test_report_flow -v
Forces EMAIL_TEST_MODE=true and a temp DB before importing app; patches
hazard.default_service and geocode.reverse_geocode so no ML/network runs.
"""

import importlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

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
import detector  # noqa: E402
import geocode  # noqa: E402
import cluster_service  # noqa: E402

from hazard import DetectionResult, DetectionService  # noqa: E402

_ML_ENV_VARS = ("ROAD_DAMAGE_INFERENCE_MODE", "ROAD_DAMAGE_ML_URL",
                "ROAD_DAMAGE_ML_TIMEOUT", "ROAD_DAMAGE_ML_RETRIES")


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
                  confidence=0.9, valid=True, accepted=None):
    """Stub whose DetectionResult mirrors what the detector really returns.

    `accepted` is filled in from the detector's own ACCEPT_THRESHOLD rather
    than left as None, because Admin-queue eligibility now depends on the
    detector's acceptance verdict: a stub that omitted it would model a
    detector that never accepts anything. valid=False still forces the
    detection threshold down, to exercise the "nothing detected" path.
    """
    if accepted is None:
        accepted = bool(
            detected and confidence is not None
            and confidence >= detector.ACCEPT_THRESHOLD)
    svc = DetectionService()
    svc.register(category, _StubModel(DetectionResult(
        detected=detected, hazard_category=category, hazard_type=htype,
        confidence=confidence, accepted=accepted)), min_conf=0.35)
    if not valid:
        svc.validate = lambda result: False
    return svc


def _stub_service_fields(damage_class, hazard_type, confidence, severity, accepted):
    """Stub carrying the Phase 2 fields exactly as RoadDamageModel.detect()
    now populates them: display-name damage_class, slug hazard_type, the
    detector's own severity, and the detector's own accepted flag."""
    svc = DetectionService()
    svc.register("road_damage", _StubModel(DetectionResult(
        detected=True, hazard_category="road_damage", hazard_type=hazard_type,
        confidence=confidence, damage_class=damage_class,
        severity=severity, accepted=accepted)), min_conf=0.35)
    return svc


class ReportFlowTest(unittest.TestCase):

    def setUp(self):
        app._rate_store.clear()
        self._reset_db()
        self.client = app.app.test_client()
        self._orig_upload_dir = app.UPLOAD_DIR
        app.UPLOAD_DIR = os.path.join(_TMP, "uploads")
        os.makedirs(app.UPLOAD_DIR, exist_ok=True)
        self._orig_default_service = hazard.default_service
        self._orig_reverse = geocode.reverse_geocode
        self._orig_get_driver = app.email_driver.get_driver
        self.recorder = _RecordingDriver()
        app.email_driver.get_driver = lambda: self.recorder

    def _reset_db(self):
        """Fresh DB + rate-limit state, so subTest loops don't accumulate rows."""
        app._rate_store.clear()
        db.DB_PATH = os.path.join(_TMP, "test.db")
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()

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

    # --- Acceptance threshold lowered 0.60 -> 0.50: route/UI behaviour ------
    # Below 50% must not reach the Admin queue, and the citizen must be told
    # why in plain language rather than being left on a dead Generate button.

    def _detect(self, **stub):
        hazard.default_service = mock.Mock(
            return_value=_stub_service(**stub))
        return self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")

    def test_detect_valid_flag_follows_the_50_percent_boundary(self):
        """`valid` is the citizen UI's Generate/Preview gate, so it must track
        the acceptance threshold, not just the 35% detection floor."""
        for confidence, expect_valid in ((0.49, False), (0.499, False),
                                         (0.50, True), (0.501, True),
                                         (0.60, True)):
            with self.subTest(confidence=confidence):
                body = self._detect(confidence=confidence).get_json()
                self.assertEqual(body["valid"], expect_valid)
                # detection itself still succeeds either way
                self.assertTrue(body["ok"])
                self.assertTrue(body["detected"])
                self.assertEqual(body["confidence"], confidence)

    def test_below_50_percent_detection_is_reported_but_marked_unaccepted(self):
        """The citizen still sees the detection result, flagged not accepted --
        the UI renders confidence + 'Validity: Invalid' from this payload."""
        body = self._detect(confidence=0.49).get_json()
        self.assertTrue(body["detected"])
        self.assertFalse(body["accepted"])
        self.assertFalse(body["valid"])
        self.assertEqual(body["confidence"], 0.49)
        self.assertEqual(body["hazard_type"], "pothole")

    def test_preview_below_50_percent_is_422_and_queues_nothing(self):
        self._reset_db()
        geocode.reverse_geocode = mock.Mock(
            return_value={"location_name": None, "authority_area": None})
        hazard.default_service = mock.Mock(return_value=_stub_service(confidence=0.49))
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(db.list_by_status("pending"), [])
        self.assertEqual(db.list_by_status("draft"), [])

    def test_preview_at_50_percent_is_allowed_and_drafts(self):
        self._reset_db()
        geocode.reverse_geocode = mock.Mock(
            return_value={"location_name": None, "authority_area": None})
        hazard.default_service = mock.Mock(return_value=_stub_service(confidence=0.50))
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        draft = db.get_detection(r.get_json()["report_id"])
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["ai_accepted"], 1)

    def test_upload_below_50_percent_queues_nothing(self):
        self._reset_db()
        hazard.default_service = mock.Mock(return_value=_stub_service(confidence=0.49))
        r = self.client.post(
            "/upload",
            data={"_upload_token": self._tok(), "source": "esp32",
                  "lat": "22.5710", "lon": "88.3639",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(db.list_by_status("pending"), [])

    def test_below_threshold_message_names_the_threshold_not_the_internals(self):
        """Plain-language copy, and it must not leak ACCEPT_THRESHOLD, the env
        var names, or any detector internals to the citizen."""
        msg = app._BELOW_ACCEPTANCE_MESSAGE
        self.assertIn("50%", msg)
        self.assertIn("clearer photo", msg)
        for leak in ("ACCEPT_THRESHOLD", "ROAD_DAMAGE_", "analyze_road",
                     "detector", "threshold=", "http://"):
            self.assertNotIn(leak, msg)

    def test_threshold_copy_tracks_the_canonical_value(self):
        """Guard against the citizen-facing '50%' drifting away from
        detector.ACCEPT_THRESHOLD if the threshold is ever changed again."""
        pct = int(round(detector.ACCEPT_THRESHOLD * 100))
        self.assertIn("%d%%" % pct, app._BELOW_ACCEPTANCE_MESSAGE)

    def test_blocked_reasons_use_distinct_citizen_copy(self):
        """Not-everything-because-of-confidence vs nothing-detected."""
        self.assertNotEqual(app._BELOW_ACCEPTANCE_MESSAGE, app._NO_DAMAGE_MESSAGE)

    def test_citizen_js_blocks_generate_and_shows_the_threshold_message(self):
        """Static wiring check on the existing UI mechanism: the detection
        result is rendered, an invalid verdict disables Next and explains why,
        and generatePreview() refuses to run without a valid detection."""
        js = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "citizen.js")
        with open(js, encoding="utf-8") as fh:
            src = fh.read()
        # message rendered through the existing detectError/status mechanism
        self.assertIn("50%", src)
        self.assertIn("detectError(msg)", src)
        self.assertIn("setDetectStatus(msg)", src)
        # result is still displayed before the verdict is applied
        self.assertIn("renderDetectionResult(d);", src)
        self.assertIn("<b>Validity:</b>", src)
        # Generate/Preview stays blocked
        self.assertIn('if (!d.valid) {', src)
        self.assertIn('$("detect-next").disabled = true;', src)
        self.assertIn("if (!state.detection || !state.detection.valid) {", src)
        # no detector internals leaked into the client
        for leak in ("ACCEPT_THRESHOLD", "ROAD_DAMAGE_ACCEPT", "analyze_road"):
            self.assertNotIn(leak, src)

    # --- Phase 2: app.py must PERSIST the detector's own values ------------
    # Before this change app.py recomputed severity as
    # detector.severity_for(<slug>), which always missed DAMAGE_SEVERITY and
    # stored "Warning" for everything, and recomputed ai_accepted as
    # `confidence >= 0.6`, which self-accepted the unclassified tier.

    def test_critical_classes_persist_detector_severity_html_route(self):
        """app.py:436-437 / :448 via POST /upload."""
        for damage_class, slug in (("Potholes", "pothole"),
                                   ("Alligator Crack", "alligator_crack")):
            with self.subTest(damage_class=damage_class):
                self._reset_db()
                hazard.default_service = mock.Mock(return_value=_stub_service_fields(
                    damage_class, slug, 0.871, "Critical", True))
                geocode.reverse_geocode = mock.Mock(return_value={
                    "location_name": None, "authority_area": None})
                r = self.client.post(
                    "/upload",
                    data={"_upload_token": self._tok(), "source": "esp32",
                          "lat": "22.5710", "lon": "88.3639",
                          "image": (_png_bytes(), "x.png")},
                    content_type="multipart/form-data")
                self.assertEqual(r.status_code, 302)
                row = db.list_by_status("pending")[0]
                # damage_class column keeps the detector's display name...
                self.assertEqual(row["damage_class"], damage_class)
                # ...while hazard_type keeps the slug normalization unchanged
                self.assertEqual(row["hazard_type"], slug)
                self.assertEqual(row["severity"], "Critical")
                self.assertEqual(row["ai_accepted"], 1)
                self.assertEqual(row["confidence"], 0.871)

    def test_unclassified_does_not_self_accept_api_route(self):
        """app.py:557-558 / :567 via POST /api/report/preview.

        confidence 0.673 here is COMBINED EVIDENCE for the unclassified tier,
        which is above the old hardcoded 0.6 -- but detector.analyze_road()
        returns accepted=False for it, and that decision must win.
        """
        self._reset_db()
        hazard.default_service = mock.Mock(return_value=_stub_service_fields(
            "Unclassified damage", "unclassified_damage", 0.673, "Warning", False))
        geocode.reverse_geocode = mock.Mock(return_value={
            "location_name": None, "authority_area": None})
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        row = db.get_detection(r.get_json()["report_id"])
        self.assertEqual(row["confidence"], 0.673)
        self.assertGreater(row["confidence"], 0.6)   # the value that used to bite
        self.assertEqual(row["ai_accepted"], 0)      # detector's decision wins
        self.assertEqual(row["damage_class"], "Unclassified damage")
        self.assertEqual(row["hazard_type"], "unclassified_damage")
        self.assertEqual(row["severity"], "Warning")

    def test_detect_response_exposes_new_fields(self):
        """to_dict() additions surface on /api/detect (additive, not breaking)."""
        hazard.default_service = mock.Mock(return_value=_stub_service_fields(
            "Potholes", "pothole", 0.871, "Critical", True))
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["hazard_type"], "pothole")   # unchanged
        self.assertEqual(body["damage_class"], "Potholes")  # new
        self.assertEqual(body["severity"], "Critical")      # new
        self.assertTrue(body["accepted"])                   # new


# ---------------------------------------------------------------------------
# Remote inference at the route level (Phase 4)
#
# The ML service itself is stubbed at the requests boundary, so these assert
# the ROUTES behave correctly when inference is remote: correct passthrough on
# success, and a clean 422 (never a fake "nothing detected") when the service
# is down. Server-authoritative preview is re-checked for the remote path.
# ---------------------------------------------------------------------------
class _FakeMLResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _ml_ok(**overrides):
    result = {"road_condition": "damaged", "damage_type": "Alligator Crack",
              "confidence": 0.871, "accepted": True,
              "bbox": [0.0, 6.7, 385.0, 516.0], "severity": "Critical"}
    result.update(overrides)
    return _FakeMLResponse(200, {"ok": True, "result": result})


class RemoteRouteTest(unittest.TestCase):
    """Route-level checks for remote inference.

    Deliberately does NOT subclass ReportFlowTest: inheriting would re-run that
    class's 13 local-mode tests inside the remote-mode fixture, which is
    misleading coverage. The small amount of setUp it needs is duplicated here.
    """

    def setUp(self):
        app._rate_store.clear()
        self._reset_db()
        self.client = app.app.test_client()
        self._orig_upload_dir = app.UPLOAD_DIR
        app.UPLOAD_DIR = os.path.join(_TMP, "uploads")
        os.makedirs(app.UPLOAD_DIR, exist_ok=True)
        self._orig_default_service = hazard.default_service
        self._orig_reverse = geocode.reverse_geocode
        self._orig_get_driver = app.email_driver.get_driver
        self.recorder = _RecordingDriver()
        app.email_driver.get_driver = lambda: self.recorder

        self._saved_env = {k: os.environ.get(k) for k in _ML_ENV_VARS}
        for k in _ML_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["ROAD_DAMAGE_INFERENCE_MODE"] = "remote"
        os.environ["ROAD_DAMAGE_ML_RETRIES"] = "0"
        # The routes reference the module object, so reloading switches them
        # over. Restore local mode and the original env afterwards.
        self.addCleanup(importlib.reload, hazard)
        self.addCleanup(self._restore_env)
        self.hazard = importlib.reload(hazard)
        self.post = mock.patch.object(
            self.hazard.requests, "post", return_value=_ml_ok()).start()
        self.addCleanup(mock.patch.stopall)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _reset_db(self):
        app._rate_store.clear()
        db.DB_PATH = os.path.join(_TMP, "test_remote.db")
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()

    def tearDown(self):
        hazard.default_service = self._orig_default_service
        geocode.reverse_geocode = self._orig_reverse
        app.email_driver.get_driver = self._orig_get_driver
        app.UPLOAD_DIR = self._orig_upload_dir

    def _tok(self):
        return self.client.get("/upload/token").get_json()["token"]

    def test_api_detect_uses_remote_service_and_returns_fields(self):
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.post.call_count, 1, "one remote call per request")
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["valid"])
        self.assertEqual(body["damage_class"], "Alligator Crack")
        self.assertEqual(body["severity"], "Critical")
        self.assertTrue(body["accepted"])
        self.assertEqual(body["bbox"], [0.0, 6.7, 385.0, 516.0])
        self.assertEqual(body["source"], "remote")

    def test_api_detect_422_when_service_is_down(self):
        """An outage must not be reported as 'no hazard found'."""
        self.post.side_effect = requests.ConnectionError("service down")
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("Detection failed", body["message"])
        self.assertNotIn("valid", body, "no verdict may be synthesised on failure")

    def test_api_detect_422_on_malformed_service_response(self):
        self.post.return_value = _FakeMLResponse(200, {"ok": True, "result": {}})
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.get_json()["ok"])

    def test_api_detect_respects_service_accept_false(self):
        """The service's accept decision wins over the confidence threshold."""
        self.post.return_value = _ml_ok(
            road_condition="damaged_unclassified", damage_type=None,
            confidence=0.673, accepted=False, severity="Warning",
            bbox=[0.0, 230.0, 423.6, 610.2])
        r = self.client.post(
            "/api/detect",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertFalse(body["accepted"])
        self.assertEqual(body["damage_class"], "Unclassified damage")
        self.assertEqual(body["severity"], "Warning")

    def test_preview_uses_remote_service_and_stays_authoritative(self):
        """Preview re-runs inference server-side; client fields are ignored."""
        self._reset_db()
        geocode.reverse_geocode = mock.Mock(
            return_value={"location_name": None, "authority_area": None})
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  # Deliberately bogus client-supplied detection values.
                  "confidence": "0.99", "accepted": "true",
                  "damage_class": "Bogus Crack", "severity": "Critical",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.post.call_count, 1,
                         "preview must call the service, not trust the client")
        row = db.get_detection(r.get_json()["report_id"])
        self.assertEqual(row["damage_class"], "Alligator Crack")
        self.assertEqual(row["severity"], "Critical")
        self.assertNotEqual(row["damage_class"], "Bogus Crack")
        self.assertTrue(row["ai_accepted"])

    def test_preview_422_and_no_draft_when_service_is_down(self):
        self._reset_db()
        geocode.reverse_geocode = mock.Mock(
            return_value={"location_name": None, "authority_area": None})
        self.post.side_effect = requests.ConnectionError("service down")
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("Detection failed", r.get_json()["message"])

    def test_preview_422_when_service_reports_normal(self):
        """A remote `normal` verdict is a real result, not a failure.

        The citizen must not be told their confidence was too low when the
        model simply saw no damage: the two blocked reasons are distinct copy.
        """
        self._reset_db()
        geocode.reverse_geocode = mock.Mock(
            return_value={"location_name": None, "authority_area": None})
        self.post.return_value = _ml_ok(
            road_condition="normal", damage_type=None, confidence=None,
            accepted=False, bbox=None, severity=None)
        r = self.client.post(
            "/api/report/preview",
            data={"_upload_token": self._tok(), "lat": "22.5710",
                  "lon": "88.3639", "location_name": "x",
                  "image": (_png_bytes(), "x.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["message"], app._NO_DAMAGE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
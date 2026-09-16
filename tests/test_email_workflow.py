"""
tests/test_email_workflow.py
---------------------------
Focused regression tests for the Track B email pipeline + Resend driver.

Run:  python -m unittest tests.test_email_workflow -v
DB isolation: each test uses its own temp SQLite file (db.DB_PATH swapped).
The workflow test proves send_authority_email() is reached through the NORMAL
application touchpoints (`corroboration.run_lifecycle_sweep()` +
`authority_routing.route_pending_clusters()` — exactly what app.py calls on
upload/approve) rather than via a direct function call.

Safety: EMAIL_TEST_MODE stays true for the workflow test (no real send, no
API key). Production-config checks stub the `resend` SDK in sys.modules so
no network request is ever made.
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

import db
import corroboration
import authority_routing
import email_driver
import lifecycle_sweep  # noqa: F401  (restored orchestrator)


class _RecordingDriver:
    """Records every would-be send without transmitting anything."""

    def __init__(self):
        self.calls = []

    def send(self, subject, recipient, body, attachment_path):
        self.calls.append({
            "subject": subject,
            "recipient": recipient,
            "body": body,
            "attachment_path": attachment_path,
        })
        return {"ok": True, "to": recipient, "mode": "recording"}


class _StubResendEmail:
    def __init__(self):
        self.sent = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "stub-id"}


class _StubResendError(Exception):
    pass


def _make_resend_stub(fail_with=None):
    """A fake `resend` module registered in sys.modules (no network). """
    resend = types.ModuleType("resend")
    resend.api_key = ""
    emails = _StubResendEmail()
    if fail_with is None:
        resend.Emails = emails
    else:
        def _send(params):
            raise fail_with
        resend.Emails = types.SimpleNamespace(send=_send)  # type: ignore[attr-defined]

    exceptions = types.ModuleType("resend.exceptions")
    exceptions.ResendError = _StubResendError
    sys.modules["resend.exceptions"] = exceptions
    return resend, emails


class EmailWorkflowTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ss_email_")
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmp, "test.db")
        db.init_db()
        self._orig_get_driver = email_driver.get_driver
        self._orig_test_mode = email_driver.TEST_MODE
        self._orig_driver = email_driver.DRIVER
        self._orig_resend_key = email_driver.RESEND_API_KEY
        self._orig_resend_from = email_driver.RESEND_FROM_EMAIL
        self._orig_resend_module = sys.modules.get("resend")
        self._orig_resend_exc_module = sys.modules.get("resend.exceptions")
        email_driver.get_driver = self._orig_get_driver
        self.recording = None

    def tearDown(self):
        email_driver.get_driver = self._orig_get_driver
        email_driver.TEST_MODE = self._orig_test_mode
        email_driver.DRIVER = self._orig_driver
        email_driver.RESEND_API_KEY = self._orig_resend_key
        email_driver.RESEND_FROM_EMAIL = self._orig_resend_from
        if self._orig_resend_module is None:
            sys.modules.pop("resend", None)
        else:
            sys.modules["resend"] = self._orig_resend_module
        if self._orig_resend_exc_module is None:
            sys.modules.pop("resend.exceptions", None)
        else:
            sys.modules["resend.exceptions"] = self._orig_resend_exc_module
        db.DB_PATH = self._orig_db_path

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _seed_corroborated_cluster(self, distinct_ips=3):
        """Insert `distinct_ips` pending detections sharing one fine cell +
        hazard + a MATURED (elapsed) window, and a verified authority for the
        coarse area. Returns (fine_key, coarse_key, window_start_dt)."""
        lat, lon = 12.9716, 77.5946
        fine_key = corroboration.fine_area_key(lat, lon)
        coarse_key = corroboration.coarse_area_key(lat, lon)
        hazard = "road_damage"

        ws = corroboration.window_start_of(
            datetime.now(timezone.utc) - timedelta(hours=4)
        )
        created = ws + timedelta(seconds=30)

        conn = db.get_conn()
        for i in range(distinct_ips):
            conn.execute(
                """
                INSERT INTO detections
                    (image_path, source, damage_class, confidence, severity,
                     lat, lon, description, status, ai_accepted, created_at,
                     client_ip, hazard_type, corroboration_area_key, authority_area_key)
                VALUES (?, 'citizen', 'pothole', 0.9, 'Warning',
                        ?, ?, 'seeded test report', 'pending', 1, ?,
                        ?, ?, ?, ?)
                """,
                (
                    os.path.join(self._tmp, "nope.jpg"),
                    lat, lon,
                    created.isoformat(),
                    f"192.0.2.{i + 1}",
                    hazard, fine_key, coarse_key,
                ),
            )
        conn.commit()
        conn.close()

        auth = db.get_or_create_authority(coarse_key, hazard, "authority@example.org")
        db.mark_authority_verified(auth["id"])
        return fine_key, coarse_key, ws

    def _run_app_touchpoints(self):
        # The exact calls app.py makes on upload/approve:
        corroboration.run_lifecycle_sweep()
        authority_routing.route_pending_clusters()

    # ------------------------------------------------------------------
    # workflow: send_authority_email reached via the app's own touchpoints
    # ------------------------------------------------------------------
    def test_workflow_reaches_send_authority_email(self):
        fine_key, coarse_key, ws = self._seed_corroborated_cluster()
        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()

        self.assertEqual(len(recorder.calls), 1, "exactly one email should be routed")
        call = recorder.calls[0]
        self.assertEqual(call["recipient"], email_driver.TEST_RECIPIENT)
        self.assertIn("Road-Damage", call["subject"])
        self.assertIn("Distinct citizen reports", call["body"])
        self.assertIn("road_damage", call["body"])
        self.assertTrue(
            call["attachment_path"] and call["attachment_path"].endswith(".pdf"),
            "letter PDF attachment should be produced",
        )
        self.assertTrue(os.path.exists(call["attachment_path"]))

        cluster = db.get_cluster_by_window(fine_key, "road_damage", ws.isoformat())
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster["lifecycle"], "emailed")
        self.assertIsNotNone(cluster["letter_path"])
        self.assertIsNotNone(cluster["emailed_at"])

    def test_workflow_does_not_resend_emailed_cluster(self):
        fine_key, coarse_key, ws = self._seed_corroborated_cluster()
        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()
        self.assertEqual(len(recorder.calls), 1)
        self._run_app_touchpoints()
        self.assertEqual(
            len(recorder.calls), 1,
            "a second sweep must not re-email an already emailed cluster",
        )

    def test_workflow_below_threshold_routes_nowhere(self):
        # 2 distinct IPs < threshold 3 -> no auto-email, cluster stays open
        fine_key, coarse_key, ws = self._seed_corroborated_cluster(distinct_ips=2)
        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()

        self.assertEqual(len(recorder.calls), 0, "below-threshold -> no email")
        cluster = db.get_cluster_by_window(fine_key, "road_damage", ws.isoformat())
        self.assertIsNone(cluster, "no cluster should be created below threshold")

    def test_same_ip_four_times_never_corroborates(self):
        # 4 rows but ONE distinct client_ip -> count stays 1 -> never emails
        fine_key, coarse_key, ws = self._seed_corroborated_cluster(distinct_ips=1)
        # _seed uses distinct IPs 192.0.2.1..; re-seed 4 rows from the SAME ip
        lat, lon = 12.9716, 77.5946
        created = ws + timedelta(seconds=30)
        conn = db.get_conn()
        for _ in range(3):
            conn.execute(
                """
                INSERT INTO detections
                    (image_path, source, damage_class, confidence, severity,
                     lat, lon, description, status, ai_accepted, created_at,
                     client_ip, hazard_type, corroboration_area_key, authority_area_key)
                VALUES (?, 'citizen', 'pothole', 0.9, 'Warning',
                        ?, ?, 'seeded test report', 'pending', 1, ?,
                        '192.0.2.1', 'road_damage', ?, ?)
                """,
                (
                    os.path.join(self._tmp, "nope.jpg"),
                    lat, lon,
                    created.isoformat(),
                    fine_key, coarse_key,
                ),
            )
        conn.commit()
        conn.close()

        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()

        self.assertEqual(len(recorder.calls), 0,
                         "one distinct IP must never reach threshold")
        cluster = db.get_cluster_by_window(fine_key, "road_damage", ws.isoformat())
        self.assertIsNone(cluster, "no cluster for a single-IP crowd")

    def test_rejected_report_never_corroborates(self):
        # 3 distinct IPs but one row is REJECTED -> confirmed rows below threshold
        lat, lon = 12.9716, 77.5946
        fine_key = corroboration.fine_area_key(lat, lon)
        coarse_key = corroboration.coarse_area_key(lat, lon)
        ws = corroboration.window_start_of(
            datetime.now(timezone.utc) - timedelta(hours=4)
        )
        created = ws + timedelta(seconds=30)

        conn = db.get_conn()
        statuses = ("pending", "pending", "rejected")
        for i, status in enumerate(statuses):
            conn.execute(
                """
                INSERT INTO detections
                    (image_path, source, damage_class, confidence, severity,
                     lat, lon, description, status, ai_accepted, created_at,
                     client_ip, hazard_type, corroboration_area_key, authority_area_key)
                VALUES (?, 'citizen', 'pothole', 0.9, 'Warning',
                        ?, ?, 'seeded test report', ?, 1, ?,
                        ?, 'road_damage', ?, ?)
                """,
                (
                    os.path.join(self._tmp, "nope.jpg"),
                    lat, lon,
                    status,
                    created.isoformat(),
                    f"192.0.2.{i + 1}",
                    fine_key, coarse_key,
                ),
            )
        conn.commit()
        conn.close()

        db.get_or_create_authority(coarse_key, "road_damage", "authority@example.org")
        row_owner = next(a for a in db.list_authorities()
                     if a["authority_area_key"] == coarse_key
                     and a["hazard_type"] == "road_damage")
        db.mark_authority_verified(row_owner["id"])

        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()

        self.assertEqual(len(recorder.calls), 0,
                         "rejected rows must be excluded from corroboration")
        cluster = db.get_cluster_by_window(fine_key, "road_damage", ws.isoformat())
        self.assertIsNone(cluster)

    def test_bounce_causes_re_route_to_admin(self):
        # After verified -> bounced (admin action), a new corroborated window
        # must NOT auto-email: it routes to the admin queue instead.
        fine_key, coarse_key, ws = self._seed_corroborated_cluster()
        auth = db.get_verified_authority(coarse_key, "road_damage")
        db.mark_authority_bounced(auth["id"], reason="test bounce")

        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()

        self.assertEqual(len(recorder.calls), 0,
                         "bounced (pending) authority must not receive email")
        cluster = db.get_cluster_by_window(fine_key, "road_damage", ws.isoformat())
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster["lifecycle"], "corroborated",
                         "unroutable cluster stays corroborated, never emailed")

    def test_bounce_flow_then_verify_emails_again(self):
        # bounce -> verify flip, then a second matured window routes to email again
        fine_key, coarse_key, ws = self._seed_corroborated_cluster()
        auth = db.get_verified_authority(coarse_key, "road_damage")
        db.mark_authority_bounced(auth["id"], reason="test")
        self.assertEqual(db.get_verified_authority(coarse_key, "road_damage"),
                         None)
        db.mark_authority_verified(auth["id"])
        self.assertIsNotNone(
            db.get_verified_authority(coarse_key, "road_damage"))

        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()
        self.assertEqual(len(recorder.calls), 1,
                         "re-verified authority resumes auto-email")

    def test_admin_queue_path_never_emails(self):
        # Verified authority exists but for a DIFFERENT coarse area -> admin_queue
        lat, lon = 12.9716, 77.5946
        fine_key = corroboration.fine_area_key(lat, lon)
        coarse_key = corroboration.coarse_area_key(lat, lon)
        ws = corroboration.window_start_of(
            datetime.now(timezone.utc) - timedelta(hours=4)
        )
        created = ws + timedelta(seconds=30)

        conn = db.get_conn()
        for i in range(3):
            conn.execute(
                """
                INSERT INTO detections
                    (image_path, source, damage_class, confidence, severity,
                     lat, lon, description, status, ai_accepted, created_at,
                     client_ip, hazard_type, corroboration_area_key, authority_area_key)
                VALUES (?, 'citizen', 'pothole', 0.9, 'Warning',
                        ?, ?, 'seeded', 'pending', 1, ?,
                        ?, 'road_damage', ?, ?)
                """,
                (
                    os.path.join(self._tmp, "nope.jpg"),
                    lat, lon,
                    created.isoformat(),
                    f"198.51.100.{i + 1}",
                    fine_key, coarse_key,
                ),
            )
        conn.commit()
        conn.close()

        # authority owned by a DIFFERENT coarse area -> unresolved -> admin queue
        db.get_or_create_authority("0.00:0.00", "road_damage", "other@example.org")

        recorder = _RecordingDriver()
        email_driver.get_driver = lambda: recorder
        email_driver.TEST_MODE = True

        self._run_app_touchpoints()
        self.assertEqual(len(recorder.calls), 0, "admin-queue route must not email")

    # ------------------------------------------------------------------
    # Resend production config checks (SDK stubbed, no network)
    # ------------------------------------------------------------------
    def test_test_recipient_is_gmail(self):
        # §3.4 gate: the default/configured test recipient must be a gmail address
        self.assertTrue(
            email_driver.TEST_RECIPIENT.endswith("@gmail.com"),
            f"EMAIL_TEST_RECIPIENT must be a gmail address, got {email_driver.TEST_RECIPIENT!r}",
        )

    def test_resend_driver_selects_and_posts_params(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "SmartSurround <notices@example.com>"
        resend, emails = _make_resend_stub()
        sys.modules["resend"] = resend

        driver = email_driver.get_driver()
        self.assertIsInstance(driver, email_driver._ResendDriver)
        result = driver.send("subject", "case@authority.gov", "body", None)

        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(resend.api_key, "re_test_key")
        self.assertEqual(len(emails.sent), 1)
        params = emails.sent[0]
        self.assertEqual(params["from"], "SmartSurround <notices@example.com>")
        self.assertEqual(params["to"], ["case@authority.gov"])
        self.assertEqual(params["subject"], "subject")
        self.assertEqual(params["text"], "body")

    def test_resend_driver_attaches_pdf(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "notices@example.com"
        resend, emails = _make_resend_stub()
        sys.modules["resend"] = resend

        letter_path = os.path.join(self._tmp, "letter.pdf")
        with open(letter_path, "wb") as fh:
            fh.write(b"%PDF-1.4 test-kb")

        driver = email_driver.get_driver()
        result = driver.send("s", "case@authority.gov", "b", letter_path)
        self.assertTrue(result["ok"], msg=result)
        params = emails.sent[0]
        self.assertEqual(len(params["attachments"]), 1)
        self.assertEqual(params["attachments"][0]["filename"], "letter.pdf")
        self.assertEqual(params["attachments"][0]["content"],
                         list(b"%PDF-1.4 test-kb"))

    def test_resend_missing_config_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = ""
        email_driver.RESEND_FROM_EMAIL = ""

        driver = email_driver.get_driver()
        self.assertIsInstance(driver, email_driver._ResendDriver)
        result = driver.send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_resend_config")

    def test_resend_api_error_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "notices@example.com"
        resend, _emails = _make_resend_stub(fail_with=_StubResendError("boom"))
        sys.modules["resend"] = resend

        driver = email_driver.get_driver()
        result = driver.send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "resend_error:_StubResendError")


if __name__ == "__main__":
    unittest.main()
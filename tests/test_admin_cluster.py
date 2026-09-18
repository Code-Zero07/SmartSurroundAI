"""
tests/test_admin_cluster.py
---------------------------
Admin-authorized cluster send orchestration: approval/send separation,
duplicate-send prevention, missing-email recovery, generic hazard categories.

Run:  python -m unittest discover -s tests -p "test_admin_cluster.py" -v
A recording driver replaces email_driver.get_driver (no network).
"""

import os
import tempfile
import unittest

import db
import corroboration
import cluster_service
import email_driver


class _RecordingDriver:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send(self, subject, recipient, body, attachment_path):
        if self.fail:
            return {"ok": False, "reason": "provider_error", "to": recipient}
        self.calls.append({
            "subject": subject, "recipient": recipient, "body": body,
            "attachment_path": attachment_path,
        })
        return {"ok": True, "to": recipient, "mode": "recording"}


class ClusterServiceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ss_service_")
        self._orig_db = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmp, "test.db")
        db.init_db()
        self._orig_get_driver = email_driver.get_driver
        self.recorder = _RecordingDriver()
        email_driver.get_driver = lambda: self.recorder
        email_driver.TEST_MODE = True
        self.lat, self.lon = 22.5710, 88.3639
        self.fine = corroboration.fine_area_key(self.lat, self.lon)
        self.coarse = corroboration.coarse_area_key(self.lat, self.lon)

    def tearDown(self):
        email_driver.get_driver = self._orig_get_driver
        db.DB_PATH = self._orig_db

    def _report(self, category="road_damage", htype="pothole", confidence=0.9):
        return db.insert_detection(
            image_path="x.jpg", source="citizen", damage_class=htype,
            confidence=confidence, severity="Warning", lat=self.lat, lon=self.lon,
            ai_accepted=1, hazard_category=category, hazard_type=htype,
            location_name="Near College More, Kolkata",
            corroboration_area_key=self.fine, authority_area_key=self.coarse)

    def _approved_or_missing(self, category="road_damage", htype="pothole"):
        rid = self._report(category=category, htype=htype)
        cid = corroboration.assign_report_to_cluster(
            rid, self.fine, category, authority_area_key=self.coarse)
        db.update_status(rid, "approved")
        corroboration.recompute_for_report(rid)
        return rid, cid

    def _add_verified_authority(self, category="road_damage"):
        auth = db.get_or_create_authority(self.coarse, category,
                                          "authority@example.gov")
        db.mark_authority_verified(auth["id"])
        return auth

    def test_approve_sends_one_email_and_marks_sent(self):
        self._add_verified_authority()
        rid, cid = self._approved_or_missing()   # review the report first
        result = cluster_service.approve_and_maybe_send(cid)
        # report review returns... note: _approved_or_missing already approved report
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "sent")
        self.assertIsNotNone(cluster["sent_at"])
        self.assertEqual(len(self.recorder.calls), 1)
        call = self.recorder.calls[0]
        self.assertIn("Road Damage", call["subject"])
        self.assertTrue(call["attachment_path"].endswith(".pdf"))
        self.assertIn(f"letter_detection_{cluster['representative_report_id']}.pdf",
                      call["attachment_path"])

    def test_approve_without_authority_goes_missing(self):
        rid, cid = self._approved_or_missing()
        result = cluster_service.approve_and_maybe_send(cid)
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "missing_authority_email")
        self.assertEqual(len(self.recorder.calls), 0)
        self.assertFalse(result["ok"])

    def test_provide_authority_email_then_send(self):
        rid, cid = self._approved_or_missing()
        cluster_service.approve_and_maybe_send(cid)
        self.assertEqual(db.get_cluster(cid)["lifecycle"], "missing_authority_email")
        email_driver.TEST_MODE = False
        result = cluster_service.provide_authority_email(cid, "authority@example.gov")
        self.assertTrue(result["ok"], msg=result)
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "sent")
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]["recipient"], "authority@example.gov")
        self.assertEqual(
            db.get_verified_authority(self.coarse, "road_damage")["email"],
            "authority@example.gov")

    def test_send_is_idempotent(self):
        self._add_verified_authority()
        rid, cid = self._approved_or_missing()
        cluster_service.approve_and_maybe_send(cid)
        result = cluster_service.send_cluster_email(cid)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.recorder.calls), 1,
                         "no second email after cluster is sent")

    def test_send_failure_stays_approved_and_records_error(self):
        self.recorder.fail = True
        self._add_verified_authority()
        rid, cid = self._approved_or_missing()
        result = cluster_service.approve_and_maybe_send(cid)
        self.assertFalse(result["ok"])
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "approved")
        self.assertIsNone(cluster["sent_at"])
        self.assertEqual(cluster["last_send_error"], "provider_error")

    def test_send_retries_after_failure(self):
        self._add_verified_authority()
        rid, cid = self._approved_or_missing()
        self.recorder.fail = True
        cluster_service.approve_and_maybe_send(cid)
        self.recorder.fail = False
        result = cluster_service.send_cluster_email(cid)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(db.get_cluster(cid)["lifecycle"], "sent")
        self.assertEqual(len(self.recorder.calls), 1)

    def test_approve_and_send_reject_terminal_states(self):
        self._add_verified_authority()
        rid, cid = self._approved_or_missing()
        cluster_service.approve_and_maybe_send(cid)
        self.assertFalse(cluster_service.approve_and_maybe_send(cid)["ok"])
        cid2_row = db.create_cluster(self.fine, "road_damage")
        db.update_cluster(cid2_row["id"], lifecycle="settled")
        self.assertFalse(cluster_service.send_cluster_email(cid2_row["id"])["ok"])
        self.assertFalse(cluster_service.approve_and_maybe_send(cid2_row["id"])["ok"])

    def test_settle_is_terminal(self):
        rid, cid = self._approved_or_missing()
        self.assertTrue(cluster_service.settle_cluster(cid)["ok"])
        self.assertEqual(db.get_cluster(cid)["lifecycle"], "settled")
        self.assertFalse(cluster_service.approve_and_maybe_send(cid)["ok"])
        self.assertFalse(cluster_service.settle_cluster(cid)["ok"])

    def test_per_report_review_never_emails(self):
        # approve/reject recompute (corroboration.recompute_for_report) must not
        # trigger any send — sends happen only via cluster_service.
        rid, cid = self._approved_or_missing()
        db.update_status(rid, "rejected")
        corroboration.recompute_for_report(rid)
        self.assertEqual(len(self.recorder.calls), 0)

    def test_generic_category_email(self):
        # Category-agnostic proof: a WATERLOGGING-flavoured hazard walks the
        # same approve -> send path (no production waterlogging model exists;
        # this only proves the pipeline is not category-specific).
        self._add_verified_authority(category="waterlogging")
        rid, cid = self._approved_or_missing(category="waterlogging",
                                             htype="waterlogged_road")
        self.assertEqual(db.get_detection(rid)["hazard_type"], "waterlogged_road")
        result = cluster_service.approve_and_maybe_send(cid)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertIn("Waterlogging", self.recorder.calls[0]["subject"])


if __name__ == "__main__":
    unittest.main()
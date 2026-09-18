"""
tests/test_clustering.py
------------------------
Geographical clustering: join active cluster, new generation after terminal,
representative = highest-confidence eligible, recompute on review change.

Run:  python -m unittest discover -s tests -p "test_clustering.py" -v
"""

import os
import tempfile
import unittest

import db
import corroboration

_tmp = None
_n = {"i": 0}


def _tick():
    _n["i"] += 1
    return _n["i"]


class ClusteringTest(unittest.TestCase):

    def setUp(self):
        global _tmp
        _tmp = tempfile.mkdtemp(prefix=f"ss_cluster_{_tick()}_")
        self._orig = db.DB_PATH
        db.DB_PATH = os.path.join(_tmp, "test.db")
        db.init_db()
        self.lat, self.lon = 22.5710, 88.3639
        self.fine = corroboration.fine_area_key(self.lat, self.lon)
        self.coarse = corroboration.coarse_area_key(self.lat, self.lon)

    def tearDown(self):
        db.DB_PATH = self._orig

    def _report(self, confidence=0.9, status="pending", hazard_type="pothole",
                hazard_category="road_damage", cluster_id=None):
        rid = db.insert_detection(
            image_path="x.jpg", source="citizen", damage_class=hazard_type,
            confidence=confidence, severity="Warning", lat=self.lat, lon=self.lon,
            ai_accepted=1, hazard_category=hazard_category, hazard_type=hazard_type,
            corroboration_area_key=self.fine, authority_area_key=self.coarse,
            status=status)
        if cluster_id is not None:
            db.set_report_cluster(rid, cluster_id)
        return rid

    def test_first_report_creates_pending_approval_cluster(self):
        rid = self._report()
        cid = corroboration.assign_report_to_cluster(
            rid, self.fine, "road_damage", authority_area_key=self.coarse)
        cluster = db.get_cluster(cid)
        self.assertEqual(cluster["lifecycle"], "pending_approval")
        self.assertEqual(cluster["representative_report_id"], rid)
        self.assertEqual(db.get_detection(rid)["cluster_id"], cid)

    def test_second_report_joins_active_cluster(self):
        r1 = self._report(confidence=0.8)
        c1 = corroboration.assign_report_to_cluster(
            r1, self.fine, "road_damage", authority_area_key=self.coarse)
        r2 = self._report(confidence=0.95)
        c2 = corroboration.assign_report_to_cluster(
            r2, self.fine, "road_damage", authority_area_key=self.coarse)
        self.assertEqual(c1, c2)
        self.assertEqual(db.get_cluster(c1)["representative_report_id"], r2)

    def test_report_after_sent_cluster_starts_new_generation(self):
        r1 = self._report()
        c1 = corroboration.assign_report_to_cluster(
            r1, self.fine, "road_damage", authority_area_key=self.coarse)
        db.update_cluster(c1, lifecycle="sent", sent_at="2026-09-18T00:00:00+00:00")

        r2 = self._report()
        c2 = corroboration.assign_report_to_cluster(
            r2, self.fine, "road_damage", authority_area_key=self.coarse)
        self.assertNotEqual(c1, c2)
        self.assertEqual(db.get_cluster(c1)["lifecycle"], "sent")
        self.assertEqual(db.get_cluster(c2)["lifecycle"], "pending_approval")

    def test_rejected_report_is_not_representative(self):
        r1 = self._report(confidence=0.99)
        cid = corroboration.assign_report_to_cluster(
            r1, self.fine, "road_damage", authority_area_key=self.coarse)
        db.update_status(r1, "rejected")
        corroboration.recompute_for_report(r1)
        self.assertIsNone(db.get_cluster(cid)["representative_report_id"])

        r2 = self._report(confidence=0.6)
        cid2 = corroboration.assign_report_to_cluster(
            r2, self.fine, "road_damage", authority_area_key=self.coarse)
        self.assertEqual(cid2, cid)
        self.assertEqual(db.get_cluster(cid)["representative_report_id"], r2)

    def test_review_change_recomputes_representative(self):
        r1 = self._report(confidence=0.9)
        r2 = self._report(confidence=0.7)
        cid = corroboration.assign_report_to_cluster(
            r1, self.fine, "road_damage", authority_area_key=self.coarse)
        corroboration.assign_report_to_cluster(
            r2, self.fine, "road_damage", authority_area_key=self.coarse)
        self.assertEqual(db.get_cluster(cid)["representative_report_id"], r1)

        # demote the representative -> recompute picks the next-highest eligible
        db.update_status(r1, "rejected")
        corroboration.recompute_for_report(r1)
        self.assertEqual(db.get_cluster(cid)["representative_report_id"], r2)

    def test_creation_race_falls_back_to_existing_active_cluster(self):
        cluster = db.create_cluster(self.fine, "road_damage",
                                    authority_area_key=self.coarse)
        original = db.get_active_cluster
        calls = {"n": 0}

        def flaky(fine_key, hazard_category):
            calls["n"] += 1
            return None if calls["n"] == 1 else original(fine_key, hazard_category)

        db.get_active_cluster = flaky
        try:
            rid = self._report()
            cid = corroboration.assign_report_to_cluster(
                rid, self.fine, "road_damage", authority_area_key=self.coarse)
        finally:
            db.get_active_cluster = original
        self.assertEqual(cid, cluster["id"])
        self.assertEqual(db.get_detection(rid)["cluster_id"], cluster["id"])

    def test_single_report_forms_cluster_and_is_representative(self):
        rid = self._report()
        cid = corroboration.assign_report_to_cluster(
            rid, self.fine, "road_damage", authority_area_key=self.coarse)
        payload = corroboration.clusters_for_admin()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["report_count"], 1)
        self.assertEqual(payload[0]["representative"]["id"], rid)


if __name__ == "__main__":
    unittest.main()
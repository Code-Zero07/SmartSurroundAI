"""
tests/test_db_migration.py
--------------------------
Exercises the additive schema migration and the new cluster data layer.

Run:  python -m unittest discover -s tests -p "test_db_migration.py" -v
DB isolation: each test uses its own temp SQLite file (db.DB_PATH swapped).
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

import db
import hazard_types

test_count = {"n": 0}


def _now():
    return datetime.now(timezone.utc).isoformat()


class DbMigrationTest(unittest.TestCase):

    def setUp(self):
        test_count["n"] += 1
        self._tmp = tempfile.mkdtemp(prefix=f"ss_db_{test_count['n']}_")
        self._orig = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmp, "test.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig

    def _legacy_detection(self, conn, cid, damage_class, hazard_type):
        conn.execute(
            """
            INSERT INTO detections
                (image_path, source, damage_class, confidence, severity,
                 lat, lon, description, status, ai_accepted, created_at,
                 client_ip, hazard_type, corroboration_area_key, authority_area_key)
            VALUES (?, 'citizen', ?, 0.9, 'Warning', 22.5710, 88.3639,
                    'legacy', 'pending', 1, ?, '192.0.2.1', ?, '22.57:88.36', '22.57:88.36')
            """,
            ("legacy.jpg", damage_class, _now(), hazard_type),
        )

    def test_legacy_detection_gets_category_and_type_slug(self):
        conn = db.get_conn()
        self._legacy_detection(conn, 1, "Potholes", "road_damage")
        conn.commit()
        conn.close()

        # Re-run init_db: migration must backfill hazard_category from the
        # legacy hazard_type and convert hazard_type to the slug.
        db.init_db()

        row = db.get_detection(1)
        self.assertEqual(row["hazard_category"], "road_damage")
        self.assertEqual(row["hazard_type"], "pothole")
        self.assertIsNone(row["cluster_id"])
        self.assertEqual(row["location_name"], None)

    def test_detections_table_has_new_columns(self):
        conn = db.get_conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(detections)")}
        conn.close()
        for col in ("hazard_category", "hazard_type", "location_name",
                    "authority_area", "detection_model", "detection_model_version",
                    "cluster_id"):
            self.assertIn(col, cols)

    def test_authorities_table_has_authority_name(self):
        conn = db.get_conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(authorities)")}
        conn.close()
        self.assertIn("authority_name", cols)

    def _legacy_cluster(self, conn):
        # Simulate a pre-migration DB: the windowed corroboration schema.
        conn.execute(
            """
            CREATE TABLE corroboration_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                corroboration_area_key TEXT NOT NULL,
                hazard_type TEXT NOT NULL,
                window_start TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'open',
                letter_path TEXT,
                emailed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(corroboration_area_key, hazard_type, window_start)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO corroboration_clusters
                (corroboration_area_key, hazard_type, window_start,
                 lifecycle, letter_path, emailed_at, created_at)
            VALUES ('22.571:88.364', 'road_damage', '2026-01-01T00:00:00+00:00',
                    'emailed', 'letter_detection_1.pdf', '2026-01-01T01:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')
            """
        )

    def test_legacy_clusters_rebuilt_as_sent_history(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE corroboration_clusters")
        self._legacy_cluster(conn)
        conn.commit()
        conn.close()

        db.init_db()

        clusters = db.list_clusters_for_admin()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["hazard_category"], "road_damage")
        self.assertEqual(clusters[0]["lifecycle"], "sent")
        self.assertIsNotNone(clusters[0]["sent_at"])
        self.assertIsNone(clusters[0]["representative_report_id"])

    def test_partial_unique_index_blocks_two_active_clusters(self):
        c1 = db.create_cluster("22.571:88.364", "road_damage")
        self.assertEqual(c1["lifecycle"], "pending_approval")
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_cluster("22.571:88.364", "road_damage")

    def test_two_sent_generations_can_coexist(self):
        c1 = db.create_cluster("22.571:88.364", "road_damage")
        db.update_cluster(c1["id"], lifecycle="sent", sent_at=_now())
        c2 = db.create_cluster("22.571:88.364", "road_damage")
        self.assertNotEqual(c1["id"], c2["id"])

    def test_get_active_cluster_ignores_terminal_rows(self):
        c1 = db.create_cluster("22.571:88.364", "road_damage")
        self.assertEqual(
            db.get_active_cluster("22.571:88.364", "road_damage")["id"], c1["id"])
        db.update_cluster(c1["id"], lifecycle="settled")
        self.assertIsNone(db.get_active_cluster("22.571:88.364", "road_damage"))

    def test_insert_detection_status_override_and_cluster_link(self):
        report_id = db.insert_detection(
            image_path="x.jpg", source="citizen", damage_class="pothole",
            confidence=0.9, severity="Warning", lat=22.57, lon=88.36,
            ai_accepted=1, hazard_category="road_damage", hazard_type="pothole",
            status="draft")
        row = db.get_detection(report_id)
        self.assertEqual(row["status"], "draft")
        db.update_status(report_id, "pending")
        db.set_report_cluster(report_id, 7)
        self.assertEqual(db.get_detection(report_id)["cluster_id"], 7)

    def test_update_cluster_supports_new_fields(self):
        c = db.create_cluster("22.571:88.364", "road_damage",
                              authority_area_key="22.57:88.36")
        db.update_cluster(c["id"], representative_report_id=3,
                          last_send_error="boom")
        c2 = db.get_cluster(c["id"])
        self.assertEqual(c2["representative_report_id"], 3)
        self.assertEqual(c2["last_send_error"], "boom")

    def test_draft_sweep_primitives(self):
        report_id = db.insert_detection(
            image_path="draft.jpg", source="citizen", damage_class="pothole",
            confidence=0.9, severity="Warning", lat=22.57, lon=88.36,
            ai_accepted=1, hazard_category="road_damage", hazard_type="pothole",
            status="draft")
        cutoff = datetime.now(timezone.utc).isoformat()
        self.assertIn(report_id, [r["id"] for r in db.list_expired_drafts(cutoff)])
        db.delete_draft(report_id)
        self.assertIsNone(db.get_detection(report_id))
        self.assertEqual(len(db.list_expired_drafts(cutoff)), 0)


if __name__ == "__main__":
    unittest.main()
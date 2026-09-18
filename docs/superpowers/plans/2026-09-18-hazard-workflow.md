# SmartSurround Extensible Hazard Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the SmartSurround reporting pipeline around a normalized hazard/detection contract: citizen location -> detection service -> draft/PDF preview -> submit -> geo-cluster -> admin approval -> single authority email, with a clean extension point for future models (e.g. waterlogging).

**Architecture:** Keep the existing single Flask + SQLite + vanilla-JS app and extend in place. Add a generic detection layer (`hazard.py`) that adapts the existing `detector.py`; repurpose `corroboration.py` into pure geographical clustering with a one-active-cluster partial-unique-index invariant; add `geocode.py` (configurable Nominatim endpoint) and `cluster_service.py` (admin-authorized send orchestration); generalize `letter_generator.py` to normalized fields; add token-gated JSON citizen endpoints and admin cluster routes; rewrite the two templates.

**Tech Stack:** Python 3.13 (Flask 3.0.3, SQLite stdlib, reportlab 4.2.2, ultralytics YOLOv8 for the existing road-damage model), vanilla JS + Leaflet/OSM via CDN, unittest. No new Python dependencies.

**Spec:** `docs/superpowers/specs/2026-09-18-hazard-workflow-design.md` — the plan argues from this spec; executors must read it alongside this plan.

## Global Constraints

(Verbatim requirements from the reviewed spec; every task below inherits these.)

1. **Coordinate separation:** lat/lon = authoritative; coarse grid key = authoritative routing key; `location_name` = citizen-editable display only; `authority_area` = server-derived label only. The frontend never submits or controls `authority_area` / routing.
2. **No `if pothole:` anywhere in the workflow** — all logic consumes normalized `hazard_category` / `hazard_type` / `confidence` / `model` / `model_version`.
3. **Clustering = organization; admin approval = authorization to send; citizen submission ≠ email.** No minimum-report counts, no corroboration threshold, no auto-send.
4. **One valid report can form a cluster.** Representative = highest-confidence report with `status IN ('pending','approved')`; rejected reports never become representatives; recompute on join and on per-report review changes.
5. **One active cluster per (fine area + hazard_category):** active = `pending_approval`/`approved`/`missing_authority_email`; enforced by a partial unique index plus a transactional re-join race backstop. `sent`/`settled` are terminal and never block a new generation; a later report after a terminal cluster starts a NEW cluster (new id → a new email, never a duplicate).
6. **`sent` is terminal and idempotent:** guarded by `sent_at IS NULL` + lifecycle check; `sent_at` written only on success; failure leaves `sent_at` null and records `last_send_error`. `settled` is terminal too; `approve`/`send` reject `sent`/`settled`.
7. **Approval and sending are separate:** setting `approved` never depends on email delivery; `/send` can safely resume after approval.
8. **Authority lookup = `coarse_area_key + hazard_category`, never `location_name`.** `authorities.hazard_type` retains the category key for migration compatibility (documented; it is *not* the normalized specific type, which lives on `detections.hazard_type`).
9. **Email:** ONE email per send, PDF letter only (representative photo embedded), only the highest-confidence eligible representative photograph; body/subject generic over hazard category. SMTP/provider acceptance is not proof of delivery (delivery/bounce state tracked separately).
10. **Reverse geocoding:** `GEOCODE_URL` env-configurable (default public Nominatim), same `geocode.py` code path, custom User-Agent + timeout, server-side only (never exposed to the frontend). Geocoder outage may affect human-readable labels only — never routing (coarse key is pure coordinate math); submission is never blocked on geocoder availability.
11. **Security:** server validates coords bounds, image type/size, `hazard_category` ∈ registry, report/cluster state, authority email format. Never trusts client-supplied `confidence`, `authority_area`, `cluster_id`, or `status`. Public endpoints use the existing single-use upload-token pattern; admin routes use `@require_auth`. Secrets stay in `.env` (untracked).
12. **No waterlogging implementation** (no model, no UI, no fake results). The generic interface lets a future `WaterloggingModel` register under `hazard_category="waterlogging"`. Tests may use a test-only stub `HazardModel`.
13. Report lifecycle `draft -> pending -> approved/rejected`; cluster lifecycle `pending_approval -> approved -> sent` (+ `missing_authority_email`, `settled`). Reuse existing status columns (`detections.status`, `corroboration_clusters.lifecycle`) — no competing status systems.

---

## File Structure

**Created:**
- `hazard_types.py` — damage-class → type-slug map + `type_slug()` (no heavy imports; usable by migration and the adapter).
- `hazard.py` — generic detection layer: `DetectionResult`, `HazardModel`, `RoadDamageModel` adapter, `DetectionService` registry + validation, per-module `default_service()`.
- `geocode.py` — configurable reverse-geocoding client (stdlib `urllib`).
- `cluster_service.py` — admin-authorized cluster send orchestration (`approve_and_maybe_send`, `send_cluster_email`, `settle_cluster`, `provide_authority_email`).
- `static/citizen.js` — 5-step citizen wizard (location/map, photo, detection, preview, submit).

**Modified:**
- `db.py` — migration (new `detections`/`authorities` columns; `corroboration_clusters` rebuild with partial unique index); new cluster/draft data-access functions; extended `insert_detection`.
- `corroboration.py` — repurposed: pure clustering (fine/coarse keys, assign-to-cluster with race backstop, representative recompute, admin payload, draft sweep). Removes windows/threshold/auto-send.
- `authority_routing.py` — removes dead `route_pending_clusters`; keeps `resolve_owner`/lifecycle flips.
- `letter_generator.py` — generalized to normalized fields; adds `humanize_label`.
- `app.py` — `_valid_image`/`_validated_coords`/`_require_upload_token` helpers; public `/api/*` endpoints; modernized `/upload`; `/admin/api/clusters` new payload; per-report approve/reject recompute; admin cluster routes (`approve`/`send`/`settle`/`authority-email`).
- `templates/index.html` — wizard markup (Leaflet CDN).
- `templates/admin.html` — cluster cards + cluster actions, new lifecycle badges.
- `tests/*` — new test modules; `tests/test_email_workflow.py` removed (threshold behavior deleted) and its driver tests moved to `tests/test_driver_contract.py`.

**Removed:** `lifecycle_sweep.py` (its orchestrator role is replaced by `cluster_service.py`).

---

## Task 1: Detection layer contract

**Files:**
- Create: `hazard_types.py`
- Create: `hazard.py`
- Test: `tests/test_hazard_contract.py`

**Interfaces:**
- Produces: `type_slug(damage_class) -> Optional[str]` (hazard_types.py); `DetectionResult` dataclass (`detected`, `hazard_category`, `hazard_type`, `confidence`, `model`, `model_version`, `.to_dict()`); `ModelInferenceError`, `UnsupportedHazardCategory`; `HazardModel` protocol (`.model_name`, `.model_version`, `.detect(image_path) -> DetectionResult`); `RoadDamageModel` (`.model_name == "road_damage_yolov8s"`, `.model_version == "v1"`); `DetectionService` (`register(category, model, min_conf=None)`, `categories()`, `supported(category) -> bool`, `detect(category, image_path)`, `min_confidence(category)`, `validate(result) -> bool`); `default_service() -> DetectionService` singleton.
- Consumes: existing `detector.analyze_road(image_path)` (unmodified).

- [ ] **Step 1: Write the failing test**

Create `tests/test_hazard_contract.py`:

```python
"""
tests/test_hazard_contract.py
-----------------------------
Regression tests for the generic detection layer (hazard.py).

Run:  python -m unittest tests.test_hazard_contract -v
The road-damage adapter is exercised against a MOCKED detector.analyze_road
so no YOLO inference ever runs. A test-only stub HazardModel proves the
workflow contract is category-agnostic.
"""

import unittest
from unittest import mock

import hazard
import hazard_types
from hazard import (
    DetectionResult, DetectionService, ModelInferenceError,
    RoadDamageModel, UnsupportedHazardCategory,
)


NORMAL = {"road_condition": "normal", "damage_type": None,
          "confidence": None, "accepted": False, "bbox": None}


class _StubModel:
    model_name = "stub"
    model_version = "v0"

    def __init__(self, result):
        self._result = result

    def detect(self, image_path):
        return self._result


class HazardContractTest(unittest.TestCase):

    def test_type_slug_mapping(self):
        self.assertEqual(hazard_types.type_slug("Potholes"), "pothole")
        self.assertEqual(hazard_types.type_slug("Alligator Crack"), "alligator_crack")
        self.assertIsNone(hazard_types.type_slug("Something Else"))

    def test_normal_image_maps_to_not_detected(self):
        with mock.patch.object(hazard.detector, "analyze_road", return_value=NORMAL):
            result = RoadDamageModel().detect("x.jpg")
        self.assertFalse(result.detected)
        self.assertEqual(result.hazard_category, "road_damage")
        self.assertIsNone(result.hazard_type)
        self.assertEqual(result.model, "road_damage_yolov8s")

    def test_damaged_image_maps_to_normalized_result(self):
        with mock.patch.object(hazard.detector, "analyze_road", return_value={
            "road_condition": "damaged", "damage_type": "Potholes",
            "confidence": 0.96, "accepted": True, "bbox": [1, 2, 3, 4],
        }):
            result = RoadDamageModel().detect("x.jpg")
        self.assertTrue(result.detected)
        self.assertEqual(result.hazard_category, "road_damage")
        self.assertEqual(result.hazard_type, "pothole")
        self.assertEqual(result.confidence, 0.96)
        self.assertEqual(result.to_dict()["model"], "road_damage_yolov8s")

    def test_unregistered_category_raises_cleanly(self):
        svc = DetectionService()
        svc.register("road_damage", _StubModel(DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type="pothole", confidence=0.9)))
        with self.assertRaises(UnsupportedHazardCategory):
            svc.detect("waterlogging", "x.jpg")

    def test_validate_uses_category_threshold(self):
        svc = DetectionService()
        svc.register("road_damage", _StubModel(DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type="pothole", confidence=0.9)), min_conf=0.35)
        self.assertTrue(svc.validate(DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type="pothole", confidence=0.4)))
        self.assertFalse(svc.validate(DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type="pothole", confidence=0.3)))
        self.assertFalse(svc.validate(DetectionResult(
            detected=False, hazard_category="road_damage",
            hazard_type=None, confidence=None)))

    def test_model_error_soft_fails(self):
        class _Boom:
            model_name = "boom"
            model_version = None

            def detect(self, image_path):
                raise RuntimeError("inference exploded")

        svc = DetectionService()
        svc.register("road_damage", _Boom())
        with self.assertRaises(ModelInferenceError):
            svc.detect("road_damage", "x.jpg")

    def test_stub_model_proves_category_agnostic_contract(self):
        svc = DetectionService()
        svc.register(
            "waterlogging",
            _StubModel(DetectionResult(
                detected=True, hazard_category="waterlogging",
                hazard_type="waterlogged_road", confidence=0.93)),
            min_conf=0.9)
        result = svc.detect("waterlogging", "x.jpg")
        self.assertTrue(svc.validate(result))
        self.assertEqual(result.to_dict()["hazard_category"], "waterlogging")
        self.assertEqual(result.to_dict()["hazard_type"], "waterlogged_road")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_hazard_contract -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hazard'`.

- [ ] **Step 3: Implement `hazard_types.py`**

```python
"""hazard_types.py
------------------
Shared hazard-type vocabulary (no heavy imports so db.py migrations can use
it without pulling in the ML stack). Maps detector class names to normalized
slugs and humanizes labels for display/email/PDF.
"""

DAMAGE_TYPE_SLUGS = {
    "Potholes": "pothole",
    "Alligator Crack": "alligator_crack",
    "Longitudinal Crack": "longitudinal_crack",
    "Transverse Crack": "transverse_crack",
    "Unclassified damage": "unclassified_damage",
}


def type_slug(damage_class):
    """Normalized hazard type for a detector class name, or None."""
    return DAMAGE_TYPE_SLUGS.get(damage_class)


def humanize_label(value):
    """'road_damage' -> 'Road Damage'; 'waterlogged_road' -> 'Waterlogged Road';
    None/'' -> 'Unspecified'."""
    if not value:
        return "Unspecified"
    return " ".join(word.capitalize() for word in str(value).replace("-", " ").split("_"))
```

- [ ] **Step 4: Implement `hazard.py`**

```python
"""hazard.py
-----------
Generic detection layer. The reporting workflow consumes ONLY the normalized
DetectionResult contract — it never knows about YOLO classes, per-model
confidence semantics, or detector internals.

A future model (e.g. waterlogging) implements the HazardModel protocol and is
registered on a DetectionService under its hazard_category; nothing else in
the pipeline changes.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol

import detector
import hazard_types


@dataclass
class DetectionResult:
    detected: bool
    hazard_category: str
    hazard_type: Optional[str] = None
    confidence: Optional[float] = None
    model: str = ""
    model_version: Optional[str] = None

    def to_dict(self):
        return {
            "detected": self.detected,
            "hazard_category": self.hazard_category,
            "hazard_type": self.hazard_type,
            "confidence": self.confidence,
            "model": self.model,
            "model_version": self.model_version,
        }


class ModelInferenceError(Exception):
    """A registered model failed to produce a result (soft-fail surface)."""


class UnsupportedHazardCategory(Exception):
    """A hazard_category with no registered model was requested."""


class HazardModel(Protocol):
    model_name: str
    model_version: Optional[str]

    def detect(self, image_path: str) -> DetectionResult:
        ...


class RoadDamageModel:
    """Adapter over the existing detector.analyze_road() (module unchanged).
    detector.py stays the only place that knows YOLO classes / severity."""

    model_name = "road_damage_yolov8s"
    model_version = "v1"

    def detect(self, image_path):
        result = detector.analyze_road(image_path)
        if result["road_condition"] == "normal":
            return DetectionResult(
                detected=False, hazard_category="road_damage",
                hazard_type=None, confidence=None,
                model=self.model_name, model_version=self.model_version,
            )
        damage_class = result["damage_type"] or "Unclassified damage"
        return DetectionResult(
            detected=True,
            hazard_category="road_damage",
            hazard_type=hazard_types.type_slug(damage_class),
            confidence=result["confidence"],
            model=self.model_name,
            model_version=self.model_version,
        )


class DetectionService:
    """Registry hazard_category -> HazardModel with per-category validation
    thresholds. Only registered categories can be processed."""

    def __init__(self):
        self._models = {}
        self._min_conf = {}

    def register(self, category, model, min_conf=None):
        self._models[category] = model
        self._min_conf[category] = min_conf

    def categories(self):
        return list(self._models)

    def supported(self, category):
        return category in self._models

    def detect(self, category, image_path):
        model = self._models.get(category)
        if model is None:
            raise UnsupportedHazardCategory(category)
        try:
            return model.detect(image_path)
        except UnsupportedHazardCategory:
            raise
        except Exception as exc:
            raise ModelInferenceError(category) from exc

    def min_confidence(self, category):
        return self._min_conf.get(category)

    def validate(self, result):
        threshold = self.min_confidence(result.hazard_category)
        if threshold is None:
            return False
        return (
            result.detected
            and result.confidence is not None
            and result.confidence >= threshold
        )


# Centralized per-category validation thresholds. Reuses the existing
# road-damage confidence default; a future category gets its own env knob.
DEFAULT_MIN_CONF = float(
    os.environ.get(
        "ROAD_DAMAGE_MIN_CONF",
        os.environ.get("ROAD_DAMAGE_CONF_THRESHOLD", "0.35"),
    )
)

_service = None


def default_service():
    """Singleton registry built from HAZARD_CATEGORIES (default road_damage).
    Unknown configured categories are left unsupported (no invented model)."""
    global _service
    if _service is None:
        _service = DetectionService()
        categories = [
            c.strip()
            for c in os.environ.get("HAZARD_CATEGORIES", "road_damage").split(",")
            if c.strip()
        ]
        for category in categories:
            if category == "road_damage":
                _service.register(category, RoadDamageModel(), min_conf=DEFAULT_MIN_CONF)
    return _service
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_hazard_contract -v`
Expected: 8 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add hazard_types.py hazard.py tests/test_hazard_contract.py
git commit -m "feat: generic detection layer with normalized DetectionResult contract"
```

---

## Task 2: DB schema migration

**Files:**
- Modify: `db.py` (all of it — see below)
- Create: `tests/test_db_migration.py`
- Delete: `tests/test_email_workflow.py`
- Create: `tests/test_driver_contract.py`

**Interfaces:**
- Consumes: `hazard_types.type_slug` (lightweight, no ML imports).
- Produces: extended `insert_detection(...)` (adds `hazard_category`, `hazard_type`, `location_name`, `authority_area`, `detection_model`, `detection_model_version`, `status` params); `CLUSTER_ACTIVE_LIFECYCLES`; `get_active_cluster(fine_key, hazard_category)`, `create_cluster(fine_key, hazard_category, authority_area_key=None, representative_report_id=None)`, `get_cluster(cluster_id)`, `set_report_cluster(report_id, cluster_id)`, `update_cluster(cluster_id, **changes)`, `list_clusters_for_admin()`, `list_expired_drafts(cutoff_iso)`, `delete_draft(report_id)`.
- Spec refs: §8, §11, §14.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration.py`:

```python
"""
tests/test_db_migration.py
--------------------------
Exercises the additive schema migration and the new cluster data layer.

Run:  python -m unittest tests.test_db_migration -v
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
            ("legacy.jpg", damage_class, _now(), hazard_type, "22.571:88.364"),
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
        self.assertIsNotNone(row["cluster_id"])
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
        self.assertEqual(len(db.list_expired_drafts(cutoff)), 0)
        db.delete_draft(report_id)
        self.assertIsNone(db.get_detection(report_id))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_db_migration -v`
Expected: FAIL (missing columns/functions).

- [ ] **Step 3: Implement the `db.py` schema changes + new data layer**

Rewrite the top docstring of `db.py` to reflect the repurposed cluster role (remove "corroboration count" language; describe one-active-cluster generations). Then apply these changes:

**Module-level constant, after imports:**

```python
CLUSTER_ACTIVE_LIFECYCLES = (
    "pending_approval",
    "approved",
    "missing_authority_email",
)

_CLUSTER_DDL = """
CREATE TABLE IF NOT EXISTS corroboration_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corroboration_area_key TEXT NOT NULL,
    hazard_category TEXT NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'pending_approval',
    representative_report_id INTEGER,
    authority_area_key TEXT,
    letter_path TEXT,
    sent_at TEXT,
    last_send_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CLUSTER_ACTIVE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cluster "
    "ON corroboration_clusters (corroboration_area_key, hazard_category) "
    "WHERE lifecycle IN ('pending_approval','approved','missing_authority_email')"
)
```

**Replace the `corroboration_clusters` block in `init_db()`** (currently lines ~107-121) with calls to a new migration function:

```python
    _migrate_hazard_schema(conn)
```

**Add the migration functions** (called from `init_db` after the base `CREATE TABLE`/`_add_column` Track-A-B block):

```python
def _migrate_hazard_schema(conn):
    # 1) additive columns on detections / authorities
    _add_column(conn, "detections", "hazard_category TEXT NOT NULL DEFAULT 'road_damage'")
    _add_column(conn, "detections", "location_name TEXT")
    _add_column(conn, "detections", "authority_area TEXT")
    _add_column(conn, "detections", "detection_model TEXT")
    _add_column(conn, "detections", "detection_model_version TEXT")
    _add_column(conn, "detections", "cluster_id INTEGER")
    _add_column(conn, "authorities", "authority_name TEXT")

    # 2) backfill hazard_category from the legacy hazard_type VALUE when that
    #    value is not already a normalized type slug (legacy rows carried the
    #    category 'road_damage' in hazard_type).
    conn.execute(
        """
        UPDATE detections
        SET hazard_category = hazard_type
        WHERE hazard_category = 'road_damage'
          AND hazard_type IS NOT NULL
          AND hazard_type NOT IN ('pothole','alligator_crack',
                                  'longitudinal_crack','transverse_crack',
                                  'unclassified_damage')
          AND hazard_type <> 'road_damage'
        """
    )

    # 3) legacy hazard_type rows still holding the category key become the
    #    normalized specific type derived from damage_class.
    rows = conn.execute(
        "SELECT id, damage_class FROM detections WHERE hazard_type = 'road_damage'"
    ).fetchall()
    for r in rows:
        slug = hazard_types.type_slug(r["damage_class"])
        if slug:
            conn.execute(
                "UPDATE detections SET hazard_type = ? WHERE id = ?",
                (slug, r["id"]),
            )

    # 4) corroboration_clusters: rebuild from the legacy windowed schema,
    #    preserving historical rows as terminal generations.
    if _has_column(conn, "corroboration_clusters", "window_start"):
        legacy = conn.execute("SELECT * FROM corroboration_clusters").fetchall()
        conn.execute("DROP TABLE corroboration_clusters")
        conn.execute(_CLUSTER_DDL)
        conn.execute(_CLUSTER_ACTIVE_INDEX_DDL)
        legacy_map = {
            "open": "pending_approval",
            "corroborated": "pending_approval",
            "emailed": "sent",
            "settled": "settled",
        }
        for r in legacy:
            lifecycle = legacy_map.get(r["lifecycle"], "pending_approval")
            sent_at = r["emailed_at"] if lifecycle == "sent" else None
            conn.execute(
                """
                INSERT INTO corroboration_clusters
                    (corroboration_area_key, hazard_category, lifecycle,
                     letter_path, sent_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (r["corroboration_area_key"], r["hazard_type"], lifecycle,
                 r["letter_path"], sent_at, r["created_at"], r["created_at"]),
            )
    else:
        conn.execute(_CLUSTER_DDL)
        conn.execute(_CLUSTER_ACTIVE_INDEX_DDL)
    conn.commit()
```

**Remove** the now-obsolete cluster-by-window functions (`get_or_create_cluster`, `get_cluster_by_window`, `update_cluster` old signature, `corroboration_count`, and the `CLUSTER_WINDOW_STEP` constant) and replace the cluster section with:

```python
def insert_detection(image_path, source, damage_class, confidence, severity,
                     lat=None, lon=None, description=None, ai_accepted=None,
                     client_ip=None, hazard_type=None, hazard_category=None,
                     location_name=None, authority_area=None,
                     detection_model=None, detection_model_version=None,
                     corroboration_area_key=None, authority_area_key=None,
                     status="pending"):
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO detections
            (image_path, source, damage_class, confidence, severity,
             lat, lon, description, ai_accepted, status, created_at,
             client_ip, hazard_type, corroboration_area_key, authority_area_key,
             hazard_category, location_name, authority_area,
             detection_model, detection_model_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_path, source, damage_class, confidence, severity,
            lat, lon, description,
            int(ai_accepted) if ai_accepted is not None else None,
            status,
            datetime.now(timezone.utc).isoformat(),
            client_ip, hazard_type, corroboration_area_key, authority_area_key,
            hazard_category or "road_damage", location_name, authority_area,
            detection_model, detection_model_version,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id
```

**Keep** `list_by_status`, `get_detection`, `update_status` unchanged. Then add the cluster/draft data layer at the end of `db.py` (replacing the old Track B cluster section):

```python
# ---------------------------------------------------------------------------
# Clusters (repurposed: geographical organization for admin review)
# ---------------------------------------------------------------------------

def get_active_cluster(fine_key, hazard_category):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM corroboration_clusters
        WHERE corroboration_area_key = ? AND hazard_category = ?
          AND lifecycle IN ('pending_approval','approved','missing_authority_email')
        ORDER BY id DESC LIMIT 1
        """,
        (fine_key, hazard_category),
    ).fetchone()
    conn.close()
    return row


def create_cluster(fine_key, hazard_category, authority_area_key=None,
                   representative_report_id=None):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO corroboration_clusters
            (corroboration_area_key, hazard_category, authority_area_key,
             representative_report_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (fine_key, hazard_category, authority_area_key,
         representative_report_id, now, now),
    )
    conn.commit()
    cluster_id = cur.lastrowid
    conn.close()
    return get_cluster(cluster_id)


def get_cluster(cluster_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM corroboration_clusters WHERE id = ?", (cluster_id,)
    ).fetchone()
    conn.close()
    return row


def set_report_cluster(report_id, cluster_id):
    conn = get_conn()
    conn.execute(
        "UPDATE detections SET cluster_id = ? WHERE id = ?",
        (cluster_id, report_id),
    )
    conn.commit()
    conn.close()


_CLUSTER_UPDATE_FIELDS = (
    "lifecycle", "letter_path", "sent_at", "last_send_error",
    "representative_report_id", "authority_area_key",
)


def update_cluster(cluster_id, **changes):
    conn = get_conn()
    fields = {k: v for k, v in changes.items() if k in _CLUSTER_UPDATE_FIELDS}
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not fields:
        conn.close()
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE corroboration_clusters SET {sets} WHERE id = ?",
        (*fields.values(), cluster_id),
    )
    conn.commit()
    conn.close()


def list_clusters_for_admin():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM corroboration_clusters ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def list_expired_drafts(cutoff_iso):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, image_path, letter_path FROM detections
        WHERE status = 'draft' AND created_at < ?
        """,
        (cutoff_iso,),
    ).fetchall()
    conn.close()
    return rows


def delete_draft(report_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM detections WHERE id = ? AND status = 'draft'", (report_id,)
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Move the surviving email-driver tests, delete the threshold suite**

Delete `tests/test_email_workflow.py` (it tests the REMOVED threshold/auto-send behavior and references the deleted `get_cluster_by_window`/`window_start` schema). Move the driver-level tests to `tests/test_driver_contract.py`:

```python
"""
tests/test_driver_contract.py
-----------------------------
Resend/smtplib driver contract tests (the old threshold auto-send workflow
was removed with the corroboration redesign; driver safety rules remain).

Run:  python -m unittest tests.test_driver_contract -v
"""

import os
import sys
import tempfile
import types
import unittest

import email_driver


class _StubResendEmail:
    def __init__(self):
        self.sent = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "stub-id"}


class _StubResendError(Exception):
    pass


def _make_resend_stub(fail_with=None):
    resend = types.ModuleType("resend")
    resend.api_key = ""
    emails = _StubResendEmail()
    if fail_with is None:
        resend.Emails = emails
    else:
        def _send(params):
            raise fail_with
        resend.Emails = types.SimpleNamespace(send=_send)
    exceptions = types.ModuleType("resend.exceptions")
    exceptions.ResendError = _StubResendError
    sys.modules["resend.exceptions"] = exceptions
    return resend, emails


class DriverContractTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ss_driver_")
        self._orig_get_driver = email_driver.get_driver
        self._orig_test_mode = email_driver.TEST_MODE
        self._orig_driver = email_driver.DRIVER
        self._orig_resend_key = email_driver.RESEND_API_KEY
        self._orig_resend_from = email_driver.RESEND_FROM_EMAIL
        self._orig_resend = sys.modules.get("resend")
        self._orig_resend_exc = sys.modules.get("resend.exceptions")

    def tearDown(self):
        email_driver.get_driver = self._orig_get_driver
        email_driver.TEST_MODE = self._orig_test_mode
        email_driver.DRIVER = self._orig_driver
        email_driver.RESEND_API_KEY = self._orig_resend_key
        email_driver.RESEND_FROM_EMAIL = self._orig_resend_from
        if self._orig_resend is None:
            sys.modules.pop("resend", None)
        else:
            sys.modules["resend"] = self._orig_resend
        if self._orig_resend_exc is None:
            sys.modules.pop("resend.exceptions", None)
        else:
            sys.modules["resend.exceptions"] = self._orig_resend_exc

    def test_test_recipient_is_gmail(self):
        self.assertTrue(
            email_driver.TEST_RECIPIENT.endswith("@gmail.com"),
            f"got {email_driver.TEST_RECIPIENT!r}",
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

        letter = os.path.join(self._tmp, "letter.pdf")
        with open(letter, "wb") as fh:
            fh.write(b"%PDF-1.4 test-kb")

        result = email_driver.get_driver().send("s", "case@authority.gov", "b", letter)
        self.assertTrue(result["ok"], msg=result)
        params = emails.sent[0]
        self.assertEqual(params["attachments"][0]["filename"], "letter.pdf")
        self.assertEqual(params["attachments"][0]["content"], list(b"%PDF-1.4 test-kb"))

    def test_resend_missing_config_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = ""
        email_driver.RESEND_FROM_EMAIL = ""
        result = email_driver.get_driver().send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_resend_config")

    def test_resend_api_error_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "notices@example.com"
        resend, _emails = _make_resend_stub(fail_with=_StubResendError("boom"))
        sys.modules["resend"] = resend
        result = email_driver.get_driver().send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "resend_error:_StubResendError")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_db_migration tests.test_driver_contract -v`
Expected: all PASS. Then run the existing auth suite to confirm nothing else regressed yet:
`python -m unittest tests.test_auth_protection -v` — note `test_admin_clusters_api_returns_json` will FAIL here because the app's clusters handler still references the old schema; that is fixed in Task 3 (next).

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_db_migration.py tests/test_driver_contract.py
git rm tests/test_email_workflow.py
git commit -m "feat(db): hazard schema migration, one-active-cluster partial unique index, draft primitives"
```

---

## Task 3: Clustering service + admin clusters payload

**Files:**
- Modify: `corroboration.py` (rewrite)
- Modify: `authority_routing.py` (remove dead delegator)
- Modify: `app.py` (`/admin/api/clusters` handler)
- Test: `tests/test_clustering.py`

**Interfaces:**
- Consumes: `db.get_active_cluster`, `db.create_cluster`, `db.get_cluster`, `db.set_report_cluster`, `db.update_cluster`, `db.list_clusters_for_admin`, `db.list_expired_drafts`, `db.delete_draft`, `db.CLUSTER_ACTIVE_LIFECYCLES`, `db.get_detection`, `db.get_conn`.
- Produces: `corroboration.fine_area_key(lat, lon)`, `corroboration.coarse_area_key(lat, lon)` (unchanged), `corroboration.assign_report_to_cluster(report_id, fine_key, hazard_category, authority_area_key=None) -> int`, `corroboration.recompute_representative(cluster_id) -> Optional[int]`, `corroboration.recompute_for_report(report_id) -> Optional[int]`, `corroboration.reports_in_cluster(cluster_id) -> list[sqlite3.Row]`, `corroboration.clusters_for_admin() -> list[dict]`, `corroboration.sweep_expired_drafts() -> int`, `corroboration.DRAFT_TTL_HOURS`.
- Spec refs: §8, §9, §10 (payload).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clustering.py`:

```python
"""
tests/test_clustering.py
------------------------
Geographical clustering: join active cluster, new generation after terminal,
representative = highest-confidence eligible, recompute on review change.

Run:  python -m unittest tests.test_clustering -v
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_clustering -v`
Expected: FAIL (functions missing).

- [ ] **Step 3: Rewrite `corroboration.py`**

Replace the entire file content (new docstring + code):

```python
"""corroboration.py
-------------------
Geographical clustering for ADMIN ORGANIZATION.

This is NOT corroboration and NOT a send authorization:
  - clustering groups nearby reports for the admin to review;
  - admin approval (cluster_service.py) is the ONLY send trigger;
  - a single valid report can form its own cluster.

Grid model (kept from the old Track B design):
  - FINE key   (~111 m cells): cluster grouping unit.
  - COARSE key (~1.1 km cells): belongs to ONE authority territory; used for
    authority routing (coarse key + hazard_category).

Cluster generations: at most one ACTIVE cluster per (fine key, hazard_category)
is enforced by the partial unique index in db.py (see CLUSTER_ACTIVE_LIFECYCLES).
`sent`/`settled` clusters are terminal: a later same-area report starts a NEW
generation instead of reopening the old one.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

import db

# --- config surfaces (env, all optional, with safe defaults) -----------------
FINE_DECIMALS = int(os.environ.get("CORROBORATION_FINE_DECIMALS", "3"))      # ~111 m
COARSE_DECIMALS = int(os.environ.get("CORROBORATION_COARSE_DECIMALS", "2"))  # ~1.1 km
DRAFT_TTL_HOURS = int(os.environ.get("DRAFT_TTL_HOURS", "24"))

ACTIVE_LIFECYCLES = db.CLUSTER_ACTIVE_LIFECYCLES
ELIGIBLE_REPORT_STATUSES = ("pending", "approved")


def _round_cell(value, decimals):
    return round(value, decimals)


def fine_area_key(lat, lon):
    if lat is None or lon is None:
        return None
    return f"{_round_cell(lat, FINE_DECIMALS)}:{_round_cell(lon, FINE_DECIMALS)}"


def coarse_area_key(lat, lon):
    if lat is None or lon is None:
        return None
    return f"{_round_cell(lat, COARSE_DECIMALS)}:{_round_cell(lon, COARSE_DECIMALS)}"


# ---------------------------------------------------------------------------
# Cluster membership
# ---------------------------------------------------------------------------

def assign_report_to_cluster(report_id, fine_key, hazard_category,
                             authority_area_key=None):
    """Join a report to the ACTIVE (fine key, hazard_category) cluster, or
    create a new generation. One-active-cluster is enforced by the partial
    unique index; a creation race is resolved by re-joining the winner."""
    cluster = db.get_active_cluster(fine_key, hazard_category)
    if cluster is None:
        try:
            cluster = db.create_cluster(
                fine_key, hazard_category, authority_area_key=authority_area_key)
        except sqlite3.IntegrityError:
            cluster = db.get_active_cluster(fine_key, hazard_category)
            if cluster is None:
                raise
    db.set_report_cluster(report_id, cluster["id"])
    recompute_representative(cluster["id"])
    return cluster["id"]


# ---------------------------------------------------------------------------
# Representative evidence (highest-confidence VALID report)
# ---------------------------------------------------------------------------

def recompute_representative(cluster_id):
    """Highest-confidence report with status IN (pending, approved) becomes the
    representative; refresh the cluster's authority_area_key from its coords."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return None
    conn = db.get_conn()
    row = conn.execute(
        """
        SELECT * FROM detections
        WHERE cluster_id = ?
          AND status IN ('pending', 'approved')
        ORDER BY (confidence IS NULL) ASC, confidence DESC, id ASC
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    conn.close()
    if row is None:
        db.update_cluster(cluster_id, representative_report_id=None,
                          authority_area_key=None)
        return None
    db.update_cluster(cluster_id,
                      representative_report_id=row["id"],
                      authority_area_key=row["authority_area_key"])
    return row["id"]


def recompute_for_report(report_id):
    """After a report joins or its review status changes, refresh the
    representative of its cluster."""
    report = db.get_detection(report_id)
    if report is None or report["cluster_id"] is None:
        return None
    return recompute_representative(report["cluster_id"])


def reports_in_cluster(cluster_id):
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT * FROM detections
        WHERE cluster_id = ?
        ORDER BY (confidence IS NULL) ASC, confidence DESC, id ASC
        """,
        (cluster_id,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Admin payload + draft hygiene
# ---------------------------------------------------------------------------

def _report_dict(row):
    if row is None:
        return None
    image_url = None
    if row["image_path"]:
        image_url = f"/uploads/{os.path.basename(row['image_path'])}"
    return {
        "id": row["id"],
        "status": row["status"],
        "image_url": image_url,
        "hazard_category": row["hazard_category"],
        "hazard_type": row["hazard_type"],
        "confidence": row["confidence"],
        "location_name": row["location_name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


def clusters_for_admin():
    out = []
    for c in db.list_clusters_for_admin():
        reports = reports_in_cluster(c["id"])
        rep = None
        if c["representative_report_id"]:
            rep = db.get_detection(c["representative_report_id"])
        out.append({
            "id": c["id"],
            "corroboration_area_key": c["corroboration_area_key"],
            "hazard_category": c["hazard_category"],
            "lifecycle": c["lifecycle"],
            "letter_path": c["letter_path"],
            "sent_at": c["sent_at"],
            "last_send_error": c["last_send_error"],
            "representative_report_id": c["representative_report_id"],
            "authority_area_key": c["authority_area_key"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "report_count": sum(1 for r in reports
                                if r["status"] in ELIGIBLE_REPORT_STATUSES),
            "representative": _report_dict(rep),
            "submissions": [_report_dict(r) for r in reports],
        })
    return out


def sweep_expired_drafts():
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=DRAFT_TTL_HOURS)).isoformat()
    drafts = db.list_expired_drafts(cutoff)
    removed = 0
    base = os.path.dirname(__file__)
    for d in drafts:
        for sub in ("uploads", "letters"):
            raw = d["image_path"] if sub == "uploads" else d["letter_path"]
            if not raw:
                continue
            try:
                p = os.path.join(base, sub, os.path.basename(raw))
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        db.delete_draft(d["id"])
        removed += 1
    return removed
```

- [ ] **Step 4: Update `authority_routing.py`**

Delete the obsolete `route_pending_clusters()` function (bottom of file). Keep `_looks_bogus`, `get_authority`, `list_authorities_for_admin`, `resolve_owner`, `verify_authority`, `bounce_authority`. Update the module docstring: remove the "auto-sent by the driver" wording; state that `resolve_owner` is the coarse-key + hazard_category lookup used by `cluster_service` after admin approval (sending is admin-authorized only).

- [ ] **Step 5: Update the `/admin/api/clusters` handler in `app.py`**

Replace the body of `admin_api_clusters()` so it uses the new payload (keep the authorities list, add `authority_name`):

```python
@app.route("/admin/api/clusters")
def admin_api_clusters():
    cluster_list = corroboration.clusters_for_admin()
    auth_list = []
    for a in authority_routing.list_authorities_for_admin():
        auth_list.append({
            "id":                a["id"],
            "authority_area_key": a["authority_area_key"],
            "hazard_type":       a["hazard_type"],
            "authority_name":    a["authority_name"],
            "email":             a["email"],
            "lifecycle":         a["lifecycle"],
            "created_at":        a["created_at"],
            "verified_at":       a["verified_at"],
            "bounced_at":        a["bounced_at"],
        })
    return jsonify({"ok": True, "clusters": cluster_list, "authorities": auth_list})
```

(`corroboration.list_clusters` has been removed; the old `AuthProtectionTest.test_admin_clusters_api_returns_json` will now pass again because the handler returns `ok` with empty lists.)

- [ ] **Step 6: Run the tests**

Run: `python -m unittest tests.test_clustering tests.test_db_migration tests.test_driver_contract tests.test_auth_protection -v`
Expected: all PASS (auth suite back to green).

- [ ] **Step 7: Commit**

```bash
git add corroboration.py authority_routing.py app.py tests/test_clustering.py
git commit -m "feat(clustering): repurpose corroboration.py into admin clustering with generations + representative"
```

---

## Task 4: Reverse geocoding

**Files:**
- Create: `geocode.py`
- Test: `tests/test_geocode.py`

**Interfaces:**
- Produces: `geocode.GEOCODE_URL`, `geocode.GEOCODE_TIMEOUT`, `geocode.GEOCODE_USER_AGENT`, `geocode.reverse_geocode(lat, lon) -> {"location_name": Optional[str], "authority_area": Optional[str]}`.
- Consumes: stdlib `urllib` only (no new dependency).
- Spec refs: §6, §14 (GEOCODE_URL).

- [ ] **Step 1: Write the failing test**

Create `tests/test_geocode.py`:

```python
"""
tests/test_geocode.py
---------------------
Reverse geocoding, network-free (urllib mocked).

Run:  python -m unittest tests.test_geocode -v
"""

import io
import json
import unittest
from unittest import mock

import geocode


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _json_body(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


class GeocodeTest(unittest.TestCase):

    @mock.patch.object(geocode, "urlopen")
    def test_reverse_geocode_returns_labels(self, urlopen):
        urlopen.return_value = _json_body({
            "address": {
                "road": "College More",
                "suburb": "Sector V",
                "city": "Kolkata",
            },
        })
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["location_name"], "College More")
        self.assertEqual(got["authority_area"], "Sector V / Kolkata")

    @mock.patch.object(geocode, "urlopen")
    def test_reverse_geocode_uses_configured_endpoint(self, urlopen):
        urlopen.return_value = _json_body({"address": {"city": "Kolkata"}})
        geocode.GEOCODE_URL = "https://geo.internal/reverse"
        try:
            geocode.reverse_geocode(1.0, 2.0)
        finally:
            geocode.GEOCODE_URL = (
                "https://nominatim.openstreetmap.org/reverse")
        url = urlopen.call_args[0][0].full_url
        self.assertTrue(url.startswith("https://geo.internal/reverse"), url)
        self.assertIn("lat=1.000000", url)
        self.assertIn("lon=2.000000", url)
        ua = urlopen.call_args[0][0].headers.get("User-Agent")
        self.assertIn("SmartSurround", ua)

    @mock.patch.object(geocode, "urlopen")
    def test_outage_yields_none_labels(self, urlopen):
        urlopen.side_effect = OSError("network unreachable")
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got, {"location_name": None, "authority_area": None})

    @mock.patch.object(geocode, "urlopen")
    def test_missing_address_yields_none_labels(self, urlopen):
        urlopen.return_value = _json_body({})
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["location_name"], None)
        self.assertEqual(got["authority_area"], None)

    @mock.patch.object(geocode, "urlopen")
    def test_area_label_falls_back_to_city_only(self, urlopen):
        urlopen.return_value = _json_body({"address": {"city": "Kolkata"}})
        got = geocode.reverse_geocode(22.5710, 88.3639)
        self.assertEqual(got["authority_area"], "Kolkata")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_geocode -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'geocode'`).

- [ ] **Step 3: Implement `geocode.py`**

```python
"""geocode.py
------------
Server-side reverse geocoding with a configurable endpoint (default: public
Nominatim; set GEOCODE_URL to a self-hosted Nominatim). The endpoint is a
SERVER-ONLY config — it is never exposed to the frontend.

Failure handling: a geocoder outage may affect human-readable labels only.
Routing never depends on this module — the coarse grid key is pure coordinate
math (server-derived). Submission is never blocked on geocoder availability.
"""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GEOCODE_URL = os.environ.get(
    "GEOCODE_URL", "https://nominatim.openstreetmap.org/reverse")
GEOCODE_TIMEOUT = float(os.environ.get("GEOCODE_TIMEOUT", "5"))
GEOCODE_USER_AGENT = os.environ.get(
    "GEOCODE_USER_AGENT", "SmartSurround/0.1 (admin@smartsurround.local)")


def _extract_fields(data):
    """location_name: the most specific readable tag. authority_area: a
    human area label built from suburb/neighbourhood + city/state."""
    addr = data.get("address") or {}
    location_name = (
        addr.get("road")
        or addr.get("pedestrian")
        or addr.get("neighbourhood")
        or addr.get("suburb")
        or addr.get("village")
        or addr.get("town")
        or addr.get("city")
    )
    area = (addr.get("suburb") or addr.get("neighbourhood")
            or addr.get("city_district"))
    city = (addr.get("city") or addr.get("town")
            or addr.get("village") or addr.get("state"))
    if area and city:
        authority_area = f"{area} / {city}"
    elif city:
        authority_area = city
    else:
        authority_area = area
    return location_name, authority_area


def reverse_geocode(lat, lon):
    """Always returns a dict: {"location_name": ..., "authority_area": ...}.
    Network/parse failures yield None labels (never raises into the caller)."""
    try:
        params = urlencode({
            "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
            "format": "jsonv2", "accept-language": "en",
        })
        url = f"{GEOCODE_URL}?{params}"
        req = Request(url, headers={"User-Agent": GEOCODE_USER_AGENT})
        with urlopen(req, timeout=GEOCODE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        location_name, authority_area = _extract_fields(data)
        return {"location_name": location_name, "authority_area": authority_area}
    except Exception:
        return {"location_name": None, "authority_area": None}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_geocode -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add geocode.py tests/test_geocode.py
git commit -m "feat(geocode): configurable reverse geocoding via GEOCODE_URL with fail-open labels"
```

---

## Task 5: Generalized letter generator

**Files:**
- Modify: `letter_generator.py`
- Test: `tests/test_letter.py`

**Interfaces:**
- Consumes: normalized report fields (`hazard_category`, `hazard_type`, `confidence`, `location_name`, `lat`, `lon`, `created_at`, image) with legacy fallback (`damage_class`, `severity`).
- Produces: `letter_generator.generate_letter(detection) -> str` (unchanged signature), `letter_generator.humanize_label(value) -> str`, `letter_generator.LETTERS_DIR`.
- Spec refs: §11 (email/PDF generic over hazard category), §10.

- [ ] **Step 1: Write the failing test**

Create `tests/test_letter.py`:

```python
"""
tests/test_letter.py
--------------------
Letter PDF is generic over hazard category/type and falls back to legacy fields.

Run:  python -m unittest tests.test_letter -v
"""

import os
import tempfile
import unittest
from unittest import mock

import letter_generator
from letter_generator import humanize_label


class LetterGeneratorTest(unittest.TestCase):

    def test_humanize_label(self):
        self.assertEqual(humanize_label("road_damage"), "Road Damage")
        self.assertEqual(humanize_label("waterlogged_road"), "Waterlogged Road")
        self.assertIsNone(humanize_label(None)) if False else None
        self.assertEqual(humanize_label(""), "Unspecified")
        self.assertEqual(humanize_label(None), "Unspecified")

    def _detection(self):
        return {
            "id": 7,
            "image_path": None,
            "damage_class": "pothole",
            "hazard_category": "road_damage",
            "hazard_type": "pothole",
            "confidence": 0.96,
            "severity": "Critical",
            "lat": 22.5710,
            "lon": 88.3639,
            "location_name": "Near College More, Sector V, Kolkata",
            "source": "citizen",
            "description": None,
            "created_at": "2026-09-18T10:00:00+00:00",
        }

    @mock.patch("letter_generator.LETTERS_DIR", new_callable=lambda: tempfile.mkdtemp(prefix="ss_letter_"))
    def test_generates_pdf_with_generic_text(self, letters_dir):
        path = letter_generator.generate_letter(self._detection())
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.getsize(path) > 0)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"Road Damage Requiring Attention", raw)
        self.assertIn(b"Pothole", raw)
        self.assertIn(b"22.5710", raw)
        self.assertNotIn(b"Report of Damaged Road Condition", raw)

    @mock.patch("letter_generator.LETTERS_DIR", new_callable=lambda: tempfile.mkdtemp(prefix="ss_letter_"))
    def test_waterlogging_fields_render_generically(self, letters_dir):
        d = self._detection()
        d["hazard_category"] = "waterlogging"
        d["hazard_type"] = "waterlogged_road"
        d["confidence"] = 0.93
        path = letter_generator.generate_letter(d)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"Waterlogging Requiring Attention", raw)
        self.assertIn(b"Waterlogged Road", raw)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_letter -v`
Expected: FAIL (old hardcoded text; `humanize_label` missing).

- [ ] **Step 3: Generalize `letter_generator.py`**

Add `humanize_label` (delegates to `hazard_types.humanize_label`) and rewrite `generate_letter` to consume normalized fields:

```python
"""letter_generator.py
----------------------
Builds a PDF letter to the authority for a hazard incident, using reportlab.
Consumes the NORMALIZED incident fields (hazard_category / hazard_type / ...)
so the same letter works for road damage, waterlogging, and future hazards.
"""

import os
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage)
from reportlab.lib.styles import getSampleStyleSheet

import hazard_types

LETTERS_DIR = os.path.join(os.path.dirname(__file__), "letters")
UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(LETTERS_DIR, exist_ok=True)

AUTHORITY_NAME = os.environ.get("AUTHORITY_NAME", "Public Works Department")
AUTHORITY_ADDRESS = os.environ.get("AUTHORITY_ADDRESS", "[Authority address here]")
SENDER_NAME = os.environ.get("SENDER_NAME", "SmartSurround Monitoring System")
SENDER_CONTACT = os.environ.get("SENDER_CONTACT", "[Your contact email]")


def humanize_label(value):
    return hazard_types.humanize_label(value)


def _val(detection, key, default=None):
    try:
        v = detection[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _resolve_image(detection):
    try:
        raw = (detection["image_path"] or "").strip()
    except (KeyError, IndexError):
        raw = ""
    if not raw:
        return None
    if os.path.isabs(raw) and os.path.exists(raw):
        return raw
    candidate = os.path.join(UPLOADS_DIR, os.path.basename(raw.replace("\\", "/")))
    return candidate if os.path.exists(candidate) else None


def generate_letter(detection) -> str:
    filename = f"letter_detection_{detection['id']}.pdf"
    filepath = os.path.join(LETTERS_DIR, filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            topMargin=2 * cm, bottomMargin=2 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm)
    styles = getSampleStyleSheet()
    story = []

    today = datetime.now().strftime("%d %B %Y")

    hazard_category = _val(detection, "hazard_category", "road_damage")
    hazard_label = humanize_label(hazard_category)
    hazard_type = _val(detection, "hazard_type",
                       _val(detection, "damage_class"))
    hazard_type_label = humanize_label(hazard_type)
    confidence = _val(detection, "confidence")
    location_name = _val(detection, "location_name")

    story.append(Paragraph(f"Date: {today}", styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(f"To,<br/>{AUTHORITY_NAME}<br/>{AUTHORITY_ADDRESS}",
                           styles["Normal"]))
    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph(
        f"<b>Subject: Report of {hazard_label} Requiring Attention</b>",
        styles["Heading3"]))
    story.append(Spacer(1, 0.4 * cm))

    lat = _val(detection, "lat", "N/A")
    lon = _val(detection, "lon", "N/A")
    source_label = ("an automated roadside monitoring post"
                    if _val(detection, "source") == "esp32"
                    else "a citizen complaint submitted through the SmartSurround portal")
    confidence_text = (f"{round(confidence * 100, 1)}%" if confidence is not None else "N/A")

    body = f"""
    Dear Sir/Madam,<br/><br/>
    This is an automated notice generated by the SmartSurround monitoring system,
    following manual verification by a system administrator.<br/><br/>
    A hazard classified as <b>{hazard_label}</b> (type: <b>{hazard_type_label}</b>,
    detection confidence: {confidence_text}) was identified via {source_label}
    at the following location:<br/><br/>
    <b>Location:</b> {location_name or "N/A"}<br/>
    <b>Latitude:</b> {lat} &nbsp;&nbsp; <b>Longitude:</b> {lon}<br/><br/>
    A verified snapshot of the affected location is attached below for reference.
    We request that the relevant maintenance team assess and address this condition
    at the earliest opportunity, particularly given the safety risk posed to the
    public.<br/><br/>
    """
    story.append(Paragraph(body, styles["Normal"]))

    if _val(detection, "description"):
        story.append(Paragraph(f"<b>Reporter's note:</b> {detection['description']}",
                               styles["Normal"]))
        story.append(Spacer(1, 0.4 * cm))

    img_path = _resolve_image(detection)
    if img_path:
        try:
            story.append(Spacer(1, 0.3 * cm))
            story.append(RLImage(img_path, width=10 * cm, height=7.5 * cm))
        except Exception:
            pass

    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph(f"Regards,<br/>{SENDER_NAME}<br/>{SENDER_CONTACT}",
                           styles["Normal"]))

    doc.build(story)
    return filepath
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_letter -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add letter_generator.py tests/test_letter.py
git commit -m "feat(letter): generic incident letter driven by normalized hazard fields"
```

---

## Task 6: Cluster send orchestration (`cluster_service.py`)

**Files:**
- Create: `cluster_service.py`
- Test: `tests/test_admin_cluster.py`

**Interfaces:**
- Consumes: `db.get_cluster`, `db.get_detection`, `db.update_cluster`, `db.get_or_create_authority`, `db.mark_authority_verified`, `corroboration.reports_in_cluster`, `authority_routing.resolve_owner`, `authority_routing._looks_bogus`, `letter_generator.generate_letter` + `humanize_label`, `email_driver.send_authority_email`.
- Produces: `cluster_service.approve_and_maybe_send(cluster_id) -> dict`, `cluster_service.send_cluster_email(cluster_id) -> dict`, `cluster_service.settle_cluster(cluster_id) -> dict`, `cluster_service.provide_authority_email(cluster_id, email) -> dict`.
- Spec refs: §10, §11 (guards + duplicate prevention + one email + only representative attachment).

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_cluster.py`:

```python
"""
tests/test_admin_cluster.py
---------------------------
Admin-authorized cluster send orchestration: approval/send separation,
duplicate-send prevention, missing-email recovery, generic hazard categories.

Run:  python -m unittest tests.test_admin_cluster -v
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

    def _approved_or_missing(self, category="road_damage"):
        rid = self._report(category=category)
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
        rid, cid = self._approved_or_missing(category="waterlogging")
        self.assertEqual(db.get_detection(rid)["hazard_type"], "waterlogged_road")
        result = cluster_service.approve_and_maybe_send(cid)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertIn("Waterlogging", self.recorder.calls[0]["subject"])


if __name__ == "__main__":
    unittest.main()
```

(Note: `_approved_or_missing` seeds an approved REPORT. The cluster is still `pending_approval` — cluster approval happens via `approve_and_maybe_send`. That is exactly the separation the spec requires.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_admin_cluster -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'cluster_service'`).

- [ ] **Step 3: Implement `cluster_service.py`**

```python
"""cluster_service.py
---------------------
Admin-authorized cluster send orchestration.

Guards enforced here (never in the route):
  - `approve` only from pending_approval; sets `approved` INDEPENDENT of send
    success; then attempts the send.
  - `send` only from approved/missing_authority_email AND sent_at IS NULL.
  - `sent` is terminal: sent_at written only on success; failure records
    last_send_error and leaves sent_at null (retryable via /send).
  - `settled` is terminal and not sendable.
  - Authority lookup: coarse grid key + hazard_category (never location_name).
  - One email per send; PDF letter only (representative photo embedded);
    only the highest-confidence eligible representative is used.
"""

import logging
from datetime import datetime, timezone

import db
import authority_routing
import email_driver
import letter_generator
import corroboration

logger = logging.getLogger("smart_surround.cluster")

SENDABLE_LIFECYCLES = ("approved", "missing_authority_email")


def _compose_subject(hazard_category, authority_area_key):
    label = letter_generator.humanize_label(hazard_category)
    return f"SmartSurround {label} Authority Notice - area {authority_area_key}"


def _compose_body(rep, fine_key, report_count):
    lat = f"{rep['lat']:.6f}" if rep["lat"] is not None else "N/A"
    lon = f"{rep['lon']:.6f}" if rep["lon"] is not None else "N/A"
    label = letter_generator.humanize_label(rep["hazard_category"])
    htype = letter_generator.humanize_label(
        rep["hazard_type"] or rep["damage_class"])
    confidence = (f"{round(rep['confidence'] * 100, 1)}%"
                  if rep["confidence"] is not None else "N/A")
    location = rep["location_name"] or rep["authority_area_key"] or "N/A"
    return (
        f"SmartSurround authority notice - {label} requiring attention.\n\n"
        f"Hazard:                       {label}\n"
        f"Type:                         {htype}\n"
        f"Confidence:                   {confidence}\n"
        f"Authority area (coarse key):  {rep['authority_area_key']}\n"
        f"Fine cluster area:            {fine_key}\n"
        f"Citizen reports in cluster:   {report_count}\n"
        f"Location:                     {location}\n"
        f"Coordinates:                  lat {lat}, lon {lon}\n\n"
        "Please find the generated authority letter attached."
    )


def _representative(cluster):
    if not cluster["representative_report_id"]:
        return None
    return db.get_detection(cluster["representative_report_id"])


def _send(cluster_id):
    """Shared resumable finalizer (used by approve and by /send retry)."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["sent_at"] is not None or cluster["lifecycle"] == "sent":
        return {"ok": False, "message": "Cluster already sent."}
    if cluster["lifecycle"] not in SENDABLE_LIFECYCLES:
        return {"ok": False,
                "message": f"Cannot send cluster in state {cluster['lifecycle']}."}
    rep = _representative(cluster)
    if rep is None or not rep["authority_area_key"]:
        return {"ok": False, "message": "Cluster has no valid representative."}
    owner = authority_routing.resolve_owner(rep["authority_area_key"],
                                            rep["hazard_category"])
    if owner is None:
        db.update_cluster(cluster_id, lifecycle="missing_authority_email",
                          last_send_error=None)
        return {"ok": False, "message": "No verified authority email for this area.",
                "state": "missing_authority_email"}
    count = sum(1 for r in corroboration.reports_in_cluster(cluster_id)
                if r["status"] in corroboration.ELIGIBLE_REPORT_STATUSES)
    letter_path = letter_generator.generate_letter(rep)
    result = email_driver.send_authority_email(
        owner["email"],
        _compose_subject(rep["hazard_category"], rep["authority_area_key"]),
        _compose_body(rep, cluster["corroboration_area_key"], count),
        letter_path,
    )
    if not result.get("ok"):
        reason = result.get("reason") or "unknown_email_error"
        db.update_cluster(cluster_id, last_send_error=reason)
        logger.warning("cluster %s send failed (soft): %s", cluster_id, reason)
        return {"ok": False, "message": "Email send failed; see cluster error.",
                "reason": reason}
    db.update_cluster(cluster_id, lifecycle="sent",
                      sent_at=datetime.now(timezone.utc).isoformat(),
                      letter_path=letter_path, last_send_error=None)
    return {"ok": True, "message": "Email sent and cluster marked sent."}


def approve_and_maybe_send(cluster_id):
    """Approve sets approved independently of send success, then attempts send."""
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] == "sent" or cluster["lifecycle"] == "settled":
        return {"ok": False,
                "message": f"Cluster is already {cluster['lifecycle']}."}
    if cluster["lifecycle"] != "pending_approval":
        return {"ok": False,
                "message": f"Cluster cannot be approved from state {cluster['lifecycle']}."}
    db.update_cluster(cluster_id, lifecycle="approved")
    return _send(cluster_id)


def send_cluster_email(cluster_id):
    return _send(cluster_id)


def settle_cluster(cluster_id):
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] in ("sent", "settled"):
        return {"ok": False,
                "message": f"Cluster is already {cluster['lifecycle']}."}
    db.update_cluster(cluster_id, lifecycle="settled")
    return {"ok": True, "message": "Cluster settled."}


def provide_authority_email(cluster_id, email):
    email = (email or "").strip()
    if authority_routing._looks_bogus(email):
        return {"ok": False, "message": "That email address looks invalid."}
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        return {"ok": False, "message": "Cluster not found."}
    if cluster["lifecycle"] not in ("approved", "missing_authority_email"):
        return {"ok": False,
                "message": f"Cannot attach email in state {cluster['lifecycle']}."}
    rep = _representative(cluster)
    if rep is None or not rep["authority_area_key"]:
        return {"ok": False, "message": "Cluster has no valid representative."}
    # Admin-confirmed for routing; delivery/bounce state stays separately
    # trackable on the authorities row (pending/verified/bounced lifecycle).
    owner = db.get_or_create_authority(rep["authority_area_key"],
                                       rep["hazard_category"], email)
    db.mark_authority_verified(owner["id"])
    db.update_cluster(cluster_id, lifecycle="approved")
    return _send(cluster_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_admin_cluster -v`
Expected: 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cluster_service.py tests/test_admin_cluster.py
git commit -m "feat(cluster-service): admin-authorized send with sent_at idempotency and missing-email recovery"
```

---

## Task 7: Public citizen API endpoints + modernized `/upload`

**Files:**
- Modify: `app.py`
- Test: `tests/test_report_flow.py`

**Interfaces:**
- Consumes: `hazard.default_service()`, `hazard` exceptions, `geocode.reverse_geocode`, `corroboration.fine_area_key/coarse_area_key/assign_report_to_cluster/sweep_expired_drafts`, `letter_generator.generate_letter` + `LETTERS_DIR`, `db.insert_detection/update_status/get_detection`, `detector.severity_for`, `_valid_image`, `_validated_coords`, `_require_upload_token`.
- Produces: routes `GET /api/geocode`, `POST /api/detect`, `POST /api/report/preview`, `GET /api/report/<id>/pdf`, `POST /api/report/<id>/submit`, and the rewritten `POST /upload`.
- Spec refs: §7, §13 (validation/tokens), §9 (`/upload` — no email).

- [ ] **Step 1: Write the failing test**

Create `tests/test_report_flow.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_report_flow -v`
Expected: FAIL (routes missing).

- [ ] **Step 3: Add validation/token helpers to `app.py`**

Add near the top (after the in-memory store definitions, before routes):

```python
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))


def _valid_image(image_file):
    ext = os.path.splitext(image_file.filename or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return False, "Unsupported image type."
    image_file.seek(0, 2)
    size = image_file.tell()
    image_file.seek(0)
    if size > MAX_IMAGE_BYTES:
        return False, "Image too large."
    return True, None


def _validated_coords():
    lat = request.form.get("lat") or None
    lon = request.form.get("lon") or None
    if lat is None and lon is None:
        return None, None, None
    try:
        lat_f = float(lat) if lat else None
        lon_f = float(lon) if lon else None
    except ValueError:
        return None, None, "Latitude/longitude must be numbers."
    if (lat_f is not None and not (-90 <= lat_f <= 90)) or \
       (lon_f is not None and not (-180 <= lon_f <= 180)):
        return None, None, "Coordinates out of range."
    return lat_f, lon_f, None


def _require_upload_token():
    tok = (request.form.get("_upload_token")
           or request.args.get("_upload_token")
           or request.headers.get("X-Upload-Token", ""))
    return _valid_upload_token(tok)
```

Add the import for the new modules near the other imports at the top of `app.py`:

```python
import hazard
import geocode
import cluster_service
```

- [ ] **Step 4: Add the public citizen endpoints to `app.py`**

Add these routes (place before the `# Auth: login flow` section or after `/upload`; order does not matter since paths are unique):

```python
@app.route("/api/geocode")
def api_geocode():
    if not _require_upload_token():
        return jsonify({"ok": False, "message": "Valid upload token required."}), 401
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "Latitude/longitude must be numbers."}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"ok": False, "message": "Coordinates out of range."}), 400
    return jsonify({"ok": True, **geocode.reverse_geocode(lat, lon)})


@app.route("/api/detect", methods=["POST"])
def api_detect():
    if not _require_upload_token():
        return jsonify({"ok": False, "message": "Valid upload token required."}), 401
    image_file = request.files.get("image")
    if not image_file or image_file.filename == "":
        return jsonify({"ok": False, "message": "An image is required."}), 400
    ok, err = _valid_image(image_file)
    if not ok:
        return jsonify({"ok": False, "message": err}), 400
    lat_f, lon_f, err = _validated_coords()
    if err:
        return jsonify({"ok": False, "message": err}), 400
    category = (request.form.get("hazard_category") or "road_damage").strip()
    svc = hazard.default_service()
    if not svc.supported(category):
        return jsonify({"ok": False,
                        "message": f"Unsupported hazard category: {category}"}), 400
    ext = os.path.splitext(image_file.filename)[1] or ".jpg"
    tmp_path = os.path.join(UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}{ext}")
    image_file.save(tmp_path)
    try:
        result = svc.detect(category, tmp_path)
    except hazard.ModelInferenceError:
        return jsonify({"ok": False,
                        "message": "Detection failed. Try a clearer photo."}), 422
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return jsonify({"ok": True, "valid": svc.validate(result),
                    **result.to_dict()})


@app.route("/api/report/preview", methods=["POST"])
def api_report_preview():
    if not _require_upload_token():
        return jsonify({"ok": False, "message": "Valid upload token required."}), 401
    corroboration.sweep_expired_drafts()  # opportunistic draft hygiene
    image_file = request.files.get("image")
    if not image_file or image_file.filename == "":
        return jsonify({"ok": False, "message": "An image is required."}), 400
    ok, err = _valid_image(image_file)
    if not ok:
        return jsonify({"ok": False, "message": err}), 400
    lat_f, lon_f, err = _validated_coords()
    if err:
        return jsonify({"ok": False, "message": err}), 400
    if lat_f is None or lon_f is None:
        return jsonify({"ok": False,
                        "message": "Latitude and longitude are required to file a report."}), 400
    category = (request.form.get("hazard_category") or "road_damage").strip()
    svc = hazard.default_service()
    if not svc.supported(category):
        return jsonify({"ok": False,
                        "message": f"Unsupported hazard category: {category}"}), 400
    location_name = (request.form.get("location_name") or "").strip() or None

    ext = os.path.splitext(image_file.filename)[1] or ".jpg"
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)
    image_file.save(saved_path)

    # AUTHORITATIVE detection: the client's detection/confidence is never
    # trusted here — the server re-runs the model on the uploaded image.
    try:
        result = svc.detect(category, saved_path)
    except hazard.ModelInferenceError:
        _remove_file(saved_path)
        return jsonify({"ok": False,
                        "message": "Detection failed. Try a clearer photo."}), 422
    if not svc.validate(result):
        _remove_file(saved_path)
        return jsonify({"ok": False,
                        "message": "No valid hazard detected above the confidence threshold."}), 422

    damage_class = result.hazard_type or "Unclassified damage"
    severity = detector.severity_for(damage_class)
    fine_key = corroboration.fine_area_key(lat_f, lon_f)
    coarse_key = corroboration.coarse_area_key(lat_f, lon_f)
    authority_area = geocode.reverse_geocode(lat_f, lon_f).get("authority_area")

    draft_id = db.insert_detection(
        image_path=os.path.basename(saved_path), source="citizen",
        damage_class=damage_class, confidence=result.confidence,
        severity=severity, lat=lat_f, lon=lon_f, description=None,
        ai_accepted=bool(result.confidence and result.confidence >= 0.6),
        client_ip=_client_ip(), hazard_category=result.hazard_category,
        hazard_type=result.hazard_type, location_name=location_name,
        authority_area=authority_area, detection_model=result.model,
        detection_model_version=result.model_version,
        corroboration_area_key=fine_key, authority_area_key=coarse_key,
        status="draft",
    )
    try:
        letter_path = letter_generator.generate_letter(db.get_detection(draft_id))
    except Exception:
        _remove_file(saved_path)
        db.delete_draft(draft_id)
        return jsonify({"ok": False,
                        "message": "Could not generate the report letter."}), 500
    db.update_status(draft_id, "draft", letter_path=os.path.basename(letter_path))

    return jsonify({"ok": True, "report_id": draft_id,
                    "pdf_url": f"/api/report/{draft_id}/pdf",
                    "detection": result.to_dict(),
                    "authority_area": authority_area})


@app.route("/api/report/<int:draft_id>/pdf")
def api_report_pdf(draft_id):
    if not _require_upload_token():
        return jsonify({"ok": False, "message": "Valid upload token required."}), 401
    detection = db.get_detection(draft_id)
    if detection is None or detection["status"] != "draft" \
       or not detection["letter_path"]:
        return "No preview PDF available.", 404
    return send_from_directory(letter_generator.LETTERS_DIR,
                               os.path.basename(detection["letter_path"]))


@app.route("/api/report/<int:draft_id>/submit", methods=["POST"])
def api_report_submit(draft_id):
    if not _require_upload_token():
        return jsonify({"ok": False, "message": "Valid upload token required."}), 401
    draft = db.get_detection(draft_id)
    if draft is None or draft["status"] != "draft":
        return jsonify({"ok": False,
                        "message": "Draft not found or already submitted."}), 400
    # Finalize the STORED draft: detection/coords/location_name are the
    # server-authoritative values captured at preview. Nothing from the client.
    fine_key = corroboration.fine_area_key(draft["lat"], draft["lon"])
    coarse_key = corroboration.coarse_area_key(draft["lat"], draft["lon"])
    db.update_status(draft_id, "pending")
    cluster_id = None
    if fine_key is not None:
        cluster_id = corroboration.assign_report_to_cluster(
            draft_id, fine_key, draft["hazard_category"],
            authority_area_key=coarse_key)
    return jsonify({"ok": True, "report_id": draft_id, "cluster_id": cluster_id,
                    "message": "Report submitted for review. No email is sent on submission."})
```

Add the tiny helper `_remove_file` next to the other helpers:

```python
def _remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass
```

- [ ] **Step 5: Rewrite `POST /upload` (keep the ESP32/direct path, generic + no email)**

```python
@app.route("/upload", methods=["POST"])
def upload():
    upload_tok = (request.form.get("_upload_token")
                  or request.args.get("_upload_token")
                  or request.headers.get("X-Upload-Token", ""))
    if not _valid_upload_token(upload_tok):
        return redirect(url_for("login"))

    image_file = request.files.get("image")
    if not image_file or image_file.filename == "":
        flash("Please choose an image to upload.")
        return redirect(url_for("index"))

    ok, err = _valid_image(image_file)
    if not ok:
        flash(err)
        return redirect(url_for("index"))

    source      = request.form.get("source", "citizen")
    lat_f, lon_f, coord_err = _validated_coords()
    if coord_err:
        flash(coord_err)
        return redirect(url_for("index"))
    category = (request.form.get("hazard_category") or "road_damage").strip()
    description = request.form.get("description") or None
    location_name = (request.form.get("location_name") or "").strip() or None
    client_ip = _client_ip()

    svc = hazard.default_service()
    if not svc.supported(category):
        flash("Unsupported hazard category.")
        return redirect(url_for("index"))

    ext = os.path.splitext(image_file.filename)[1] or ".jpg"
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)
    image_file.save(saved_path)

    try:
        result = svc.detect(category, saved_path)
    except hazard.ModelInferenceError:
        _remove_file(saved_path)
        flash("Detection failed. Try a clearer photo.")
        return redirect(url_for("index"))
    if not svc.validate(result):
        _remove_file(saved_path)
        flash("No valid hazard detected above the confidence threshold \u2014 nothing queued.")
        return redirect(url_for("index"))

    damage_class = result.hazard_type or "Unclassified damage"
    severity = detector.severity_for(damage_class)
    fine_key = corroboration.fine_area_key(lat_f, lon_f)
    coarse_key = corroboration.coarse_area_key(lat_f, lon_f)
    authority_area = None
    if lat_f is not None:
        authority_area = geocode.reverse_geocode(lat_f, lon_f).get("authority_area")

    new_id = db.insert_detection(
        image_path=os.path.basename(saved_path), source=source,
        damage_class=damage_class, confidence=result.confidence,
        severity=severity, lat=lat_f, lon=lon_f, description=description,
        ai_accepted=bool(result.confidence and result.confidence >= 0.6),
        client_ip=client_ip, hazard_category=result.hazard_category,
        hazard_type=result.hazard_type, location_name=location_name,
        authority_area=authority_area, detection_model=result.model,
        detection_model_version=result.model_version,
        corroboration_area_key=fine_key, authority_area_key=coarse_key,
    )
    if fine_key is not None:
        corroboration.assign_report_to_cluster(
            new_id, fine_key, result.hazard_category, authority_area_key=coarse_key)

    flash(f"Report #{new_id} ({result.hazard_type or 'hazard'}, {severity}) queued for review. "
          "No email is sent for citizen submissions \u2014 an authorized review notifies the authority.")
    return redirect(url_for("index"))
```

Remove the obsolete `emailed = corroboration.run_lifecycle_sweep()` call and its flash branches from the old `/upload`.

- [ ] **Step 6: Run the test suite**

Run: `python -m unittest tests.test_report_flow -v`
Expected: 12 tests PASS. Then confirm the auth/migration/driver suites still green:
`python -m unittest tests.test_auth_protection tests.test_db_migration tests.test_driver_contract tests.test_clustering tests.test_geocode tests.test_letter tests.test_hazard_contract -v`

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_report_flow.py
git commit -m "feat(api): token-gated citizen endpoints (geocode/detect/preview/submit) and generic /upload, no email"
```

---

## Task 8: Admin cluster routes + per-report recompute

**Files:**
- Modify: `app.py`
- Delete: `lifecycle_sweep.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: `cluster_service.approve_and_maybe_send`, `cluster_service.send_cluster_email`, `cluster_service.settle_cluster`, `cluster_service.provide_authority_email`, `corroboration.recompute_for_report`.
- Produces: routes `POST /admin/cluster/<id>/approve`, `/send`, `/settle`, `/authority-email`; updated `POST /admin/approve/<id>` and `/admin/reject/<id>` (recompute representative, no lifecycle sweep).
- Spec refs: §10, §11.

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_api.py`:

```python
"""
tests/test_admin_api.py
-----------------------
HTTP-level admin cluster routes: approval, send idempotency, settle, and
missing-authority-email recovery.

Run:  python -m unittest tests.test_admin_api -v
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
        return login.headers.get("X-Auth-Token")

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_admin_api -v`
Expected: FAIL (route 404s).

- [ ] **Step 3: Add admin cluster routes + update existing approve/reject**

Replace the `approve()` and `reject()` route bodies in `app.py` (remove the `run_lifecycle_sweep` call; add representative recompute):

```python
@app.route("/admin/approve/<int:detection_id>", methods=["POST"])
@require_auth
def approve(detection_id):
    detection = db.get_detection(detection_id)
    if detection is None:
        return jsonify({"ok": False, "message": "Detection not found."}), 404
    letter_path = letter_generator.generate_letter(detection)
    db.update_status(detection_id, "approved", letter_path=letter_path)
    corroboration.recompute_for_report(detection_id)
    return jsonify({"ok": True,
                    "message": f"Detection #{detection_id} approved \u2014 letter generated."})


@app.route("/admin/reject/<int:detection_id>", methods=["POST"])
@require_auth
def reject(detection_id):
    detection = db.get_detection(detection_id)
    if detection is None:
        return jsonify({"ok": False, "message": "Detection not found."}), 404
    db.update_status(detection_id, "rejected")
    corroboration.recompute_for_report(detection_id)
    return jsonify({"ok": True, "message": f"Detection #{detection_id} rejected."})
```

Add the four cluster routes after `bounce_authority`:

```python
@app.route("/admin/cluster/<int:cluster_id>/approve", methods=["POST"])
@require_auth
def admin_cluster_approve(cluster_id):
    result = cluster_service.approve_and_maybe_send(cluster_id)
    return _cluster_result(result)


@app.route("/admin/cluster/<int:cluster_id>/send", methods=["POST"])
@require_auth
def admin_cluster_send(cluster_id):
    result = cluster_service.send_cluster_email(cluster_id)
    return _cluster_result(result)


@app.route("/admin/cluster/<int:cluster_id>/settle", methods=["POST"])
@require_auth
def admin_cluster_settle(cluster_id):
    result = cluster_service.settle_cluster(cluster_id)
    return _cluster_result(result)


@app.route("/admin/cluster/<int:cluster_id>/authority-email", methods=["POST"])
@require_auth
def admin_cluster_authority_email(cluster_id):
    email = (request.form.get("email") or "").strip()
    result = cluster_service.provide_authority_email(cluster_id, email)
    return _cluster_result(result)
```

Add the shared response helper next to them:

```python
def _cluster_result(result):
    ok = result.get("ok", False)
    message = result.get("message", "")
    status = 200 if ok else (404 if message.startswith("Cluster not found") else 400)
    return jsonify(result), status
```

- [ ] **Step 4: Remove the dead orchestrator**

Delete `lifecycle_sweep.py` (`git rm lifecycle_sweep.py`). Nothing in the app imports it now (`corroboration.run_lifecycle_sweep` was removed in Task 3; `app.py` no longer calls it). Verify with:

`rg -n "lifecycle_sweep|run_lifecycle_sweep|route_pending_clusters" *.py`

Expected: no matches.

- [ ] **Step 5: Run the test suite**

Run: `python -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_admin_api.py
git rm lifecycle_sweep.py
git commit -m "feat(admin): cluster approve/send/settle/authority-email routes; per-report review recomputes representative only"
```

---

## Task 9: Citizen wizard frontend

**Files:**
- Modify: `templates/index.html` (rewrite into a 5-step wizard)
- Create: `static/citizen.js`

**Interfaces:**
- Consumes: `GET /upload/token`, `GET /api/geocode?lat=&lon=&_upload_token=`, `POST /api/detect`, `POST /api/report/preview`, `GET /api/report/<id>/pdf?_upload_token=`, `POST /api/report/<id>/submit`; Leaflet + OSM tile CDN. Backend now requires multiple single-use tokens (one per API call) — the JS fetches a fresh `/upload/token` before each call.
- Produces: full citizen reporting wizard (location/map, photo, detection, preview, submit). Generic Hazard / Type / Confidence UI; `location_name` editable; `authority_area` read-only and server-derived.

- [ ] **Step 1: Rewrite `templates/index.html`**

Keep the existing inline-style aesthetic and flash/secure-banner blocks. New body structure:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SmartSurround — Report a Hazard</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 720px;
           margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
    h1 { font-size: 1.4rem; }
    .note { background: #eef7f0; border-left: 3px solid #2f9e44; padding: 10px 14px;
            font-size: 0.9rem; margin-bottom: 20px; }
    .step { display: none; }
    .step.active { display: block; }
    .step-nav { display: flex; gap: 8px; margin-bottom: 20px; }
    .step-nav button { padding: 8px 14px; border: 1px solid #ccc; border-radius: 6px;
                       background: #fff; cursor: pointer; font-weight: 600; }
    .step-nav button.active-step { background: #1971c2; color: white; border-color: #1971c2; }
    label { font-size: 0.85rem; font-weight: 600; display: block; margin-top: 12px; }
    input[type=text] { padding: 8px; border: 1px solid #ccc; border-radius: 6px; width: 100%; box-sizing: border-box; }
    button { padding: 10px 16px; border: none; border-radius: 6px; background: #2f9e44;
             color: white; font-weight: 600; cursor: pointer; }
    button[type=button] { background: #3b5bdb; }
    button:disabled { opacity: 0.6; cursor: default; }
    #map { height: 260px; border-radius: 8px; margin-top: 10px; }
    .result-card { background: #f1f3f5; border-left: 3px solid #1971c2; padding: 12px 16px;
                   border-radius: 6px; margin-top: 12px; line-height: 1.6; }
    .result-card .field { font-size: 0.9rem; }
    .bad { background: #f8d7da; border-left-color: #c92a2a; }
    .flash { background: #fff3bf; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; }
    #secure-banner, #loc-status, #form-error { display: block; font-size: 0.9rem; }
    a.admin-link { display: inline-block; margin-top: 24px; font-size: 0.9rem; }
    #preview-frame { width: 100%; height: 520px; border: 1px solid #ccc; border-radius: 6px;
                     margin-top: 12px; }
    #camera-ui video, #camera-ui img { max-width: 100%; height: auto; background: #000;
                                       border-radius: 6px; display: block; margin-bottom: 8px; }
    .readonly-area { color: #555; }
  </style>
</head>
<body>
  <h1>SmartSurround — Report a Hazard</h1>
  <div class="note" id="intro-note">
    Report a road hazard or other danger near you. Your photos are reviewed
    before any authority is notified.
  </div>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endif %}
  {% endwith %}

  <div id="secure-banner" hidden>
    Camera and location require HTTPS — serve over localhost or a TLS URL.
  </div>

  <nav class="step-nav" id="step-nav">
    <button type="button" data-step="1" class="active-step">1 Location</button>
    <button type="button" data-step="2">2 Photo</button>
    <button type="button" data-step="3">3 Detection</button>
    <button type="button" data-step="4">4 Preview</button>
    <button type="button" data-step="5">5 Submit</button>
  </nav>

  <!-- Step 1: Location -->
  <section class="step active" id="step-1">
    <label>Your location</label>
    <div class="row">
      <button type="button" id="loc-button" hidden>Use my location</button>
      <span id="loc-status"></span>
    </div>
    <label>Latitude</label>
    <input type="text" id="lat" placeholder="22.5726">
    <label>Longitude</label>
    <input type="text" id="lon" placeholder="88.3639">
    <div id="map"></div>
    <label>Location name (editable)</label>
    <input type="text" id="location_name" placeholder="Near College More, Sector V, Kolkata">
    <p class="readonly-area">Authority area: <span id="authority_area">—</span>
      <small>(assigned by the system from your coordinates)</small></p>
    <div class="step-nav">
      <button type="button" id="loc-next" disabled>Next: Photo</button>
    </div>
  </section>

  <!-- Step 2: Photo -->
  <section class="step" id="step-2">
    <label>Take or choose a photo</label>
    <div id="photo-picker">
      <button type="button" id="take-photo">Take Photo</button>
      <button type="button" id="use-file">Use an existing photo</button>
    </div>
    <div id="camera-ui" hidden>
      <video id="camera-preview" playsinline autoplay muted></video>
      <canvas id="capture-canvas" hidden></canvas>
      <img id="snapshot-img" alt="Captured photo" hidden>
      <div>
        <button type="button" id="capture-btn" disabled>Capture</button>
        <button type="button" id="retake-btn" hidden>Retake</button>
      </div>
    </div>
    <div class="step-nav">
      <button type="button" id="photo-back">Back</button>
      <button type="button" id="photo-next" disabled>Next: Detect</button>
    </div>
  </section>

  <!-- Step 3: Detection -->
  <section class="step" id="step-3">
    <label>Detection</label>
    <p>Analyzing your photo against active hazard models (currently: road damage).</p>
    <button type="button" id="detect-btn">Run Detection</button>
    <div id="detection-result" class="result-card" hidden></div>
    <p id="detect-error" class="bad" hidden style="padding:10px 14px;border-radius:6px;"></p>
    <div class="step-nav">
      <button type="button" id="detect-back">Back</button>
      <button type="button" id="detect-next" disabled>Next: Generate PDF</button>
    </div>
  </section>

  <!-- Step 4: Preview -->
  <section class="step" id="step-4">
    <label>Report letter — preview</label>
    <button type="button" id="generate-btn">Generate PDF</button>
    <iframe id="preview-frame" hidden></iframe>
    <div class="step-nav">
      <button type="button" id="preview-back">Back</button>
      <button type="button" id="preview-next" disabled>Next: Submit</button>
    </div>
  </section>

  <!-- Step 5: Submit -->
  <section class="step" id="step-5">
    <p id="submit-summary"></p>
    <button type="button" id="submit-btn">Submit Report</button>
    <p id="form-error" hidden style="background:#f8d7da;border-left:3px solid #c92a2a;
        padding:10px 14px;border-radius:6px;"></p>
    <p id="submit-done" hidden style="background:#eef7f0;border-left:3px solid #2f9e44;
        padding:12px 16px;border-radius:6px;">
      <b>Report submitted for review.</b> Your report is now queued; it will not be emailed
      until it has been verified.
    </p>
  </section>

  <p id="form-error" hidden></p>
  <a class="admin-link" href="/admin">Go to admin queue &rarr;</a>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
          integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script src="/static/citizen.js" defer></script>
</body>
</html>
```

- [ ] **Step 2: Implement `static/citizen.js`**

```javascript
/* SmartSurround citizen wizard (5 steps).
 *
 * Every API call needs a FRESH single-use upload token (they are consumed).
 * The backend is authoritative for detection/coords/routing:
 *   - location_name is editable, authority_area is NOT (server-derived).
 *   - preview re-runs the model; submit finalizes the stored draft only.
 * No email is sent anywhere on the citizen path.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var nav = Array.prototype.slice.call(document.querySelectorAll("#step-nav button"));
  var steps = [1, 2, 3, 4, 5].map(function (n) { return $("step-" + n); });

  var state = {
    lat: null, lon: null, locationName: "", authorityArea: null,
    imageFile: null, detection: null, draftId: null
  };

  function token() {
    return fetch("/upload/token")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return d && d.ok ? d.token : ""; });
  }

  function showStep(n) {
    steps.forEach(function (s, i) { s.classList.toggle("active", i + 1 === n); });
    nav.forEach(function (b, i) { b.classList.toggle("active-step", i + 1 === n); });
    window.scrollTo(0, 0);
  }

  function error(msg) {
    var el = $("form-error");
    el.textContent = msg;
    el.hidden = false;
  }
  function clearError() { $("form-error").hidden = true; }
  function detectError(msg) {
    var el = $("detect-error");
    el.textContent = msg;
    el.hidden = false;
  }

  // ---- Location -----------------------------------------------------------
  var marker = null, map = null;
  function putLatLon(lat, lon) {
    state.lat = lat; state.lon = lon;
    $("lat").value = lat.toFixed(6);
    $("lon").value = lon.toFixed(6);
    if (map && marker) marker.setLatLng([lat, lon]);
    else if (map) marker = L.marker([lat, lon], { draggable: true }).addTo(map);
    if (map) map.setView([lat, lon], 16);
    geocode(lat, lon);
    $("loc-next").disabled = false;
  }

  function geocode(lat, lon) {
    token().then(function (t) {
      if (!t) return;
      return fetch("/api/geocode?lat=" + lat + "&lon=" + lon + "&_upload_token=" + t)
        .then(function (r) { return r.json(); });
    }).then(function (d) {
      if (!d) return;
      state.authorityArea = d.authority_area || null;
      $("authority_area").textContent = d.authority_area || "Area " +
        Math.round(lat * 100) / 100 + ":" + Math.round(lon * 100) / 100;
      if (d.location_name) $("location_name").value = d.location_name;
    });
  }

  function initMap() {
    if (typeof L === "undefined") return;
    map = L.map("map").setView([22.5726, 88.3639], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© OpenStreetMap"
    }).addTo(map);
    map.on("click", function (e) { putLatLon(e.latlng.lat, e.latlng.lng); });
  }

  function requestGeo() {
    if (!navigator.geolocation) { $("loc-status").textContent = "Geolocation unavailable"; return; }
    $("loc-status").textContent = "Getting your location…";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        $("loc-status").textContent = "Location set (±" + Math.round(pos.coords.accuracy) + "m)";
        putLatLon(pos.coords.latitude, pos.coords.longitude);
      },
      function (err) {
        $("loc-status").textContent = err.code === err.PERMISSION_DENIED
          ? "Location blocked — check browser site settings" : "Couldn't get a fix — tap again";
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 });
  }

  // ---- Photo (ported from the old app.js flow) -----------------------------
  var video = $("camera-preview"), canvas = $("capture-canvas"),
      snapshotImg = $("snapshot-img");
  var activeStream = null, capturedBlob = null, fileAlt = null;

  function stopStream() {
    if (activeStream) { activeStream.getTracks().forEach(function (t) { t.stop(); }); activeStream = null; }
    if (video) video.srcObject = null;
  }
  function startCamera() {
    stopStream();
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (stream) {
        activeStream = stream; video.muted = true; video.srcObject = stream; return video.play();
      })
      .then(function () {
        $("camera-ui").hidden = false; $("take-photo").hidden = true;
        $("capture-btn").disabled = false; $("snapshot-img").hidden = true;
      })
      .catch(function () { openFilePicker("Camera unavailable — selecting a file instead."); });
  }
  function onCapture() {
    var w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(video, 0, 0, w, h);
    canvas.toBlob(function (blob) {
      if (!blob) { clearError(); error("Capture failed — retake."); return; }
      capturedBlob = blob;
      state.imageFile = capturedBlob;
      stopStream();
      snapshotImg.src = URL.createObjectURL(blob);
      snapshotImg.hidden = false; video.hidden = true;
      $("capture-btn").disabled = true; $("retake-btn").hidden = false;
      $("photo-next").disabled = false;
    }, "image/jpeg", 0.92);
  }
  function ensureFileAlt() {
    if (fileAlt) return fileAlt;
    fileAlt = document.createElement("input");
    fileAlt.type = "file"; fileAlt.accept = "image/*";
    fileAlt.addEventListener("change", function () {
      if (fileAlt.files && fileAlt.files[0]) {
        state.imageFile = fileAlt.files[0];
        capturedBlob = null;
        $("photo-next").disabled = false;
      }
    });
    document.body.appendChild(fileAlt);
    return fileAlt;
  }
  function openFilePicker(message) {
    if (message) { clearError(); error(message); }
    var alt = ensureFileAlt(); alt.click(); alt.hidden = false;
  }

  // ---- API calls ------------------------------------------------------------
  function detectImage() {
    clearError(); detectError(""); $("detect-error").hidden = true;
    $("detect-btn").disabled = true;
    var fd = new FormData();
    fd.append("image", state.imageFile, "report.jpg");
    fd.append("lat", state.lat); fd.append("lon", state.lon);
    token().then(function (t) {
      if (!t) { $("detect-btn").disabled = false; return; }
      fd.append("_upload_token", t);
      return fetch("/api/detect", { method: "POST", body: fd });
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      $("detect-btn").disabled = false;
      if (!d.ok) { detectError(d.message || "Detection failed."); return; }
      state.detection = d;
      $("detection-result").hidden = false;
      $("detection-result").innerHTML =
        "<div class='field'><b>Hazard:</b> " + (d.hazard_category || "—").replace(/_/g, " ") +
        "</div><div class='field'><b>Type:</b> " + (d.hazard_type || "—").replace(/_/g, " ") +
        "</div><div class='field'><b>Confidence:</b> " +
        (d.confidence != null ? Math.round(d.confidence * 100) + "%" : "—") + "</div>";
      if (!d.valid) {
        detectError("No sufficiently valid hazard detected — try a clearer photo.");
        $("detect-next").disabled = true;
      } else {
        $("detect-next").disabled = false;
      }
    });
  }

  var draftSummary = null;
  function generatePreview() {
    $("generate-btn").disabled = true;
    var fd = new FormData();
    fd.append("image", state.imageFile, "report.jpg");
    fd.append("lat", state.lat); fd.append("lon", state.lon);
    fd.append("location_name", $("location_name").value.trim() ||
              $("location_name").placeholder);
    token().then(function (t) {
      if (!t) { $("generate-btn").disabled = false; return; }
      fd.append("_upload_token", t);
      return fetch("/api/report/preview", { method: "POST", body: fd });
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      $("generate-btn").disabled = false;
      if (!d.ok) { clearError(); error(d.message || "Could not generate the letter."); return; }
      state.draftId = d.report_id;
      draftSummary = d;
      token().then(function (t) {
        if (!t) return;
        $("preview-frame").src = "/api/report/" + d.report_id + "/pdf?_upload_token=" + t;
        $("preview-frame").hidden = false;
        $("preview-next").disabled = false;
      });
    });
  }

  function submitReport() {
    var fd = new FormData();
    $("submit-btn").disabled = true;
    token().then(function (t) {
      if (!t) { $("submit-btn").disabled = false; return; }
      fd.append("_upload_token", t);
      return fetch("/api/report/" + state.draftId + "/submit", { method: "POST", body: fd });
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) { $("submit-btn").disabled = false; error(d.message || "Submission failed."); return; }
      $("submit-summary").textContent =
        "Draft #" + d.report_id + " finalized and queued for review.";
      $("submit-done").hidden = false;
      $("submit-btn").hidden = true;
    });
  }

  // ---- Navigation wiring ----------------------------------------------------
  $("loc-button").addEventListener("click", requestGeo);
  $("location_name").addEventListener("input", function () { state.locationName = this.value; });
  $("loc-next").addEventListener("click", function () { showStep(2); });
  $("photo-back").addEventListener("click", function () { showStep(1); });
  $("take-photo").addEventListener("click", function () {
    if (window.isSecureContext && navigator.mediaDevices) startCamera();
    else openFilePicker("Camera needs HTTPS — selecting a file instead.");
  });
  $("use-file").addEventListener("click", function () { stopStream(); openFilePicker(); });
  $("capture-btn").addEventListener("click", onCapture);
  $("retake-btn").addEventListener("click", function () { stopStream(); startCamera(); });
  $("photo-next").addEventListener("click", function () { showStep(3); });
  $("detect-back").addEventListener("click", function () { showStep(2); });
  $("detect-btn").addEventListener("click", detectImage);
  $("detect-next").addEventListener("click", function () { showStep(4); });
  $("preview-back").addEventListener("click", function () { showStep(3); });
  $("generate-btn").addEventListener("click", generatePreview);
  $("preview-next").addEventListener("click", function () { showStep(5); });
  $("submit-btn").addEventListener("click", submitReport);
  nav.forEach(function (b) { b.addEventListener("click", function () { showStep(+b.dataset.step); }); });

  if (!window.isSecureContext) $("secure-banner").hidden = false;
  if (navigator.geolocation) $("loc-button").hidden = false;
  initMap();
})();
```

- [ ] **Step 3: Manual verification**

Start the server (`python app.py`) and walk the wizard in a browser over a secure context (localhost counts):
1. Location: "Use my location" fills lat/lon, populates `location_name`, sets read-only `authority_area`; dragging the map marker re-geocodes both.
2. Photo: camera capture and file upload both work; next step stays disabled until a photo is chosen.
3. Detection: shows Hazard/Type/Confidence from the normalized JSON; invalid shows a blocking message.
4. Preview: generates the draft, embeds the PDF iframe; letter shows generic "Road Damage / Pothole" content with the photo.
5. Submit: shows only "Report submitted for review"; no email is sent (check `/admin/api/clusters` — the report appears in a `pending_approval` cluster).
Also verify an ESP32-style POST to `/upload` (curl with an image + `_upload_token`) queues a `pending` report without sending mail.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/citizen.js
git commit -m "feat(citizen-wizard): 5-step reporting flow with Leaflet map, generic detection card, PDF preview, no-email submit"
```

---

## Task 10: Admin cluster cards + actions

**Files:**
- Modify: `templates/admin.html`

**Interfaces:**
- Consumes: `GET /admin/api/clusters` payload from `corroboration.clusters_for_admin()` (fields: `id`, `corroboration_area_key`, `hazard_category`, `lifecycle`, `letter_path`, `sent_at`, `last_send_error`, `representative_report_id`, `authority_area_key`, `created_at`, `updated_at`, `report_count`, `representative` (report dict or null), `submissions` (report dicts)); protected actions `POST /admin/cluster/<id>/approve|send|settle|authority-email`; existing `data-action` wrapper (`apiPost`/`handleProtected`).
- Produces: admin cluster card view with representative thumbnail, expandable submissions, state actions, and missing-email inline form.

- [ ] **Step 1: Update the cluster rendering + actions in `admin.html`**

1. Replace `lifecycleBadge()` with the new lifecycle set:

```javascript
var CLUSTER_BADGE_COLORS = {
  pending_approval: "open",
  approved: "corroborated",
  missing_authority_email: "corroborated",
  sent: "emailed",
  settled: "settled"
};
function lifecycleBadge(lc) {
  var cls = CLUSTER_BADGE_COLORS[lc] || "open";
  return '<span class="lifecycle ' + cls + '">' + (lc || "pending_approval") + "</span>";
}
```

2. Replace the cluster rows in `loadClusters()` with card rendering. Remove the old `window_start`/`emailStatusBadge` usage. Sketch (drop-in for the `c.map(...)` table body and the wrap assignment):

```javascript
function reportThumb(r) {
  if (!r || !r.image_url) return "";
  var conf = r.confidence != null ? Math.round(r.confidence * 100) + "%" : "—";
  var type = r.hazard_type || "—";
  return '<div class="report-thumb"><img src="' + r.image_url + '" alt="report photo">' +
         '<div class="meta">' + type + " · " + conf +
         '<br>#' + r.id + ' <span class="lifecycle ' + (r.status === "rejected" ? "settled" : "open") + '">' +
         r.status + "</span></div></div>";
}

function clusterCard(row) {
  var rep = row.representative;
  var repHtml = rep
    ? reportThumb(rep).replace('<div class="meta">', '<div class="meta"><b>Representative</b><br>')
    : '<p>No eligible representative.</p>';
  var others = row.submissions || [];
  var otherCount = others.length - (rep ? 1 : 0);
  var othersHtml = "";
  if (otherCount > 0) {
    var items = others
      .filter(function (r) { return !rep || r.id !== rep.id; })
      .map(reportThumb).join("");
    othersHtml = '<button type="button" class="expand-sub" data-cluster="' + row.id + '">▼ View ' +
                 otherCount + ' other submission(s)</button><div class="sub-grid" id="subs-' + row.id + '" hidden>' +
                 items + "</div>";
  }

  var actions = "";
  if (row.lifecycle === "pending_approval") {
    actions = '<button class="approve protected" data-action="cluster-approve" data-id="' + row.id + '">Approve Cluster</button> ' +
              '<button class="reject protected" data-action="cluster-settle" data-id="' + row.id + '">Settle (no send)</button>';
  } else if (row.lifecycle === "approved" || row.lifecycle === "missing_authority_email") {
    if (row.last_send_error) {
      actions += '<p class="send-error">Last send error: <code>' + row.last_send_error + "</code></p>";
    }
    actions += '<button class="approve protected" data-action="cluster-send" data-id="' + row.id + '">Send to Authority</button>';
    actions += '<div class="form-inline" style="margin-top:6px;">' +
               '<input type="text" id="auth-email-' + row.id + '" placeholder="authority@example.org">' +
               '<button class="add-auth protected" data-action="cluster-authority-email" data-id="' + row.id + '">Set email &amp; send</button></div>';
  } else if (row.lifecycle === "missing_authority_email") {
    actions += '<div class="form-inline" style="margin-top:6px;">' +
               '<input type="text" id="auth-email-' + row.id + '" placeholder="authority@example.org">' +
               '<button class="add-auth protected" data-action="cluster-authority-email" data-id="' + row.id + '">Set email &amp; send</button></div>';
  } else if (row.lifecycle === "sent") {
    actions += '<span class="lifecycle emailed">SENT' +
               (row.sent_at ? " " + row.sent_at.slice(0, 19).replace("T", " ") : "") + "</span>" +
               (row.letter_path ? ' <a href="#letter-' + row.id + '">letter recorded</a>' : "");
  } else if (row.lifecycle === "settled") {
    actions += '<span class="lifecycle settled">SETTLED — no send</span>';
  }

  return '<div class="card cluster-card">' +
    '<div class="meta">' +
      "<div><b>Cluster #" + row.id + "</b> &nbsp;" +
      '<span class="mono">' + (row.corroboration_area_key || "—") + "</span> &nbsp; " +
      lifecycleBadge(row.lifecycle) + "</div>" +
      "<div>Hazard: <b>" + (row.hazard_category || "—").replace(/_/g, " ") + "</b> &nbsp; " +
      "Reports: <b>" + row.report_count + "</b></div>" +
      "<div class='mono'>Authority area: " + (row.authority_area_key || "—") + "</div>" +
      repHtml +
      othersHtml +
    "</div>" +
    '<div class="actions">' + actions + "</div>" +
    "</div>";
}
```

3. In `loadClusters()`, replace the table HTML block with:

```javascript
var html = c.length
  ? c.map(clusterCard).join("")
  : "<p>No clusters yet — submitted reports appear here after review.</p>";
clustersWrap.innerHTML = html;

document.querySelectorAll(".expand-sub").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var t = document.getElementById("subs-" + btn.dataset.cluster);
    var hidden = t.hidden;
    t.hidden = !hidden;
    btn.textContent = hidden ? "▲ Hide other submissions" : "▼ View other submissions";
  });
});
```

4. Wire the new actions in the click handler:

```javascript
else if (action === "cluster-approve")
  handleProtected("/admin/cluster/" + id + "/approve", "Cluster approved; email attempted.");
else if (action === "cluster-send")
  handleProtected("/admin/cluster/" + id + "/send", "Send attempted.");
else if (action === "cluster-settle")
  handleProtected("/admin/cluster/" + id + "/settle", "Cluster settled.");
else if (action === "cluster-authority-email") {
  var em = document.getElementById("auth-email-" + id).value.trim();
  if (!em) { toast("Enter an authority email.", "warn"); return; }
  var fd = new FormData();
  fd.append("email", em);
  handleProtected("/admin/cluster/" + id + "/authority-email", "Email set; send attempted.", fd);
}
```

5. Add small CSS for `.report-thumb`, `.sub-grid`, `.send-error`, `.expand-sub` (grid/thumb sizing consistent with `.card img`).

- [ ] **Step 2: Manual verification**

Login at `/admin`, register an authority for a coarse area, and with a pending cluster:
- Pending card shows representative + expandable submissions; per-report photos all clickable.
- Approve Cluster with a verified authority → badge flips to `sent` with timestamp, exactly one email sent (check `EMAIL_TEST_MODE` log).
- Approve Cluster with no authority → `missing_authority_email`; the inline "Set email & send" validates, saves, marks verified, then sends once.
- Send again on a sent cluster → rejected message, no second email.
- Settle → `settled`, approve/send reject.
- Rejecting the representative report in the pending queue moves the badge to the next-highest-confidence eligible report.

- [ ] **Step 3: Commit**

```bash
git add templates/admin.html
git commit -m "feat(admin-ui): cluster cards with representative, submissions, and state actions"
```

---

## Task 11: Docs, dead-code cleanup, and full suite

**Files:**
- Modify: `README.md` (env docs), `.env.example` if present, `email_driver.py` (remove dead `mark_letter_recorded`)
- Test: full `unittest` suite run

**Spec refs:** §14 (env documentation).

- [ ] **Step 1: Update environment/config documentation**

In `README.md`, update the env table. Add (verbatim names):
- `HAZARD_CATEGORIES` — comma-separated registered hazard categories (default `road_damage`).
- `GEOCODE_URL` — reverse-geocoding endpoint (default `https://nominatim.openstreetmap.org/reverse`); **server-side only**, never exposed to the frontend; set to a self-hosted Nominatim for production.
- `GEOCODE_TIMEOUT`, `GEOCODE_USER_AGENT` — geocoding call timeout (s) and User-Agent header.
- `ROAD_DAMAGE_MIN_CONF` — per-category validation threshold for `road_damage` (default reuses `ROAD_DAMAGE_CONF_THRESHOLD`, 0.35); per-category thresholds are centralized in `hazard.py`.
- `DRAFT_TTL_HOURS` — abandoned-draft sweep age (default 24).
- `MAX_IMAGE_BYTES` — image size cap for uploads (default 10 MiB).

Mark the old Track B knobs as legacy/no-longer-used: `CORROBORATION_THRESHOLD`, `CORROBORATION_WINDOW_SECONDS` (auto-send removed; clustering no longer uses thresholds or windows).

If `.env.example` exists on disk (it is gitignored), apply the same edits there.

- [ ] **Step 2: Remove dead code**

- `email_driver.py`: delete `mark_letter_recorded()` (it calls the non-existent `db.mark_cluster_recorded` and is no longer referenced).
- Confirm no remaining references: `rg -n "mark_letter_recorded|mark_cluster_recorded|run_lifecycle_sweep|route_pending_clusters|window_start|corroboration_count|CORROBORATION_THRESHOLD" --glob "*.py"` → only expected hits in legacy docs.

- [ ] **Step 3: Run the FULL test suite**

Run: `python -m unittest discover -s tests -v`
Expected: all suites PASS:
- `test_hazard_contract` (8)
- `test_db_migration` (10)
- `test_clustering` (8)
- `test_geocode` (5)
- `test_letter` (3)
- `test_admin_cluster` (11)
- `test_report_flow` (12)
- `test_admin_api` (7)
- `test_driver_contract` (5)
- `test_auth_protection` (12)

Fix any stragglers before committing.

- [ ] **Step 4: Commit**

```bash
git add README.md email_driver.py
git commit -m "docs(env): document hazard workflow configuration; remove dead email bookkeeping"
```

---

## Self-Review

**1. Spec coverage.** Mapping spec → tasks:
- Generic detection layer + contract + registry/validation + extension point (§5) → Task 1.
- Reverse geocoding `GEOCODE_URL` + fail-open labels (§6, §14) → Task 4.
- Citizen endpoints + drafts + token gating + no email (§7) → Task 7.
- Data model incl. `hazard_category`/`hazard_type` backfill, cluster rebuild + partial unique index (§8) → Task 2 and 3.
- Clustering semantics incl. generations + representative recompute (§9) → Task 3.
- Admin cluster actions + per-report review + payload (§10) → Tasks 8 and 10.
- Authority routing + duplicate-prevention + generic email (§11) → Task 6.
- Citizen wizard + admin UI (§12) → Tasks 9 and 10.
- Security/validation (§13) → Tasks 7 (helpers) and 8 (`@require_auth`).
- Env documentation (§14) → Task 11.
- Testing outline (§15) → Tasks 1–10 test files; old threshold suite removed in Task 2.

**2. Placeholder scan:** every step contains concrete code or a concrete command; no TBDs. Manual-verification steps are explicit (no automated JS harness exists in this repo).

**3. Type consistency:** `DetectionResult` fields and `to_dict()` keys match the JSON the frontend reads (`hazard_category`, `hazard_type`, `confidence`, ...). `corroboration.clusters_for_admin()` keys (`id`, `lifecycle`, `representative`, `submissions`, `report_count`, ...) exactly match what `admin.html` Task 10 renders. `cluster_service` function names match the Task 8 routes that call them. `update_cluster(**changes)` field whitelist matches what `cluster_service` passes (`lifecycle`, `sent_at`, `letter_path`, `last_send_error`, `representative_report_id`, `authority_area_key`). `insert_detection` extended params match Task 7 call sites. No name drift found.

**4. Notable sequence decisions:** Task 3 lands right after the schema tasks so `/admin/api/clusters` (which the auth suite polls) is repaired before green-checkpoint gaps; `tests/test_email_workflow.py` is removed in Task 2 (its covered behavior is deleted, its driver tests survive in `test_driver_contract.py`); `lifecycle_sweep.py` is deleted in Task 8 after all importers are gone.
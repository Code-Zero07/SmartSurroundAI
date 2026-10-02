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


def _verdict(condition, damage_type, confidence, accepted, bbox=None):
    return {"road_condition": condition, "damage_type": damage_type,
            "confidence": confidence, "accepted": accepted, "bbox": bbox}


class _StubModel:
    model_name = "stub"
    model_version = "v0"

    def __init__(self, result):
        self._result = result

    def detect(self, image_path):
        return self._result


class HazardContractTest(unittest.TestCase):
    """The LOCAL adapter: needs hazard.detector, so it must run in local mode.

    These tests used to rely on whatever inference mode hazard happened to be
    imported in. That is not safe: tests/test_admin_api.py imports app.py at
    collection time, and app.py calls load_dotenv() on .env, which sets
    ROAD_DAMAGE_INFERENCE_MODE in os.environ for the whole process. If .env
    selects remote mode, hazard is imported without a detector module and every
    test here dies with AttributeError. setUp therefore establishes local mode
    explicitly and restores the prior module state afterwards.
    """

    def setUp(self):
        self.addCleanup(_reload_hazard)
        _local()

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

    # --- Phase 2: damage_class / severity / accepted ----------------------
    # Regression cover for the bug where app.py called
    # detector.severity_for(<slug>), which misses DAMAGE_SEVERITY entirely and
    # silently downgraded every Potholes/Alligator Crack report to "Warning".

    def test_critical_classes_keep_critical_severity(self):
        for damage_type, slug, severity in (
            ("Potholes", "pothole", "Critical"),
            ("Alligator Crack", "alligator_crack", "Critical"),
            ("Longitudinal Crack", "longitudinal_crack", "Warning"),
        ):
            with self.subTest(damage_type=damage_type):
                with mock.patch.object(hazard.detector, "analyze_road",
                                       return_value=_verdict(
                                           "damaged", damage_type, 0.8, True, [1, 2, 3, 4])):
                    result = RoadDamageModel().detect("x.jpg")
                # display name preserved, slug normalization unchanged
                self.assertEqual(result.damage_class, damage_type)
                self.assertEqual(result.hazard_type, slug)
                self.assertEqual(result.severity, severity)
                self.assertTrue(result.accepted)

    def test_severity_is_not_derived_from_the_slug(self):
        """severity_for() keys on display names; feeding it a slug yields
        DEFAULT_SEVERITY. This pins the distinction hazard.py must preserve."""
        self.assertEqual(hazard.detector.severity_for("Potholes"), "Critical")
        self.assertEqual(hazard.detector.severity_for("pothole"), "Warning")

    def test_unclassified_keeps_detectors_rejected_accepted_flag(self):
        """confidence 0.673 is COMBINED EVIDENCE, not a class confidence. The
        detector returns accepted=False ("never auto-accept an unclassified
        case"); re-deriving `confidence >= 0.6` would flip it to True."""
        with mock.patch.object(hazard.detector, "analyze_road",
                               return_value=_verdict(
                                   "damaged_unclassified", None, 0.673, False, [0, 1, 2, 3])):
            result = RoadDamageModel().detect("x.jpg")
        self.assertTrue(result.detected)
        self.assertEqual(result.damage_class, "Unclassified damage")
        self.assertEqual(result.hazard_type, "unclassified_damage")
        self.assertEqual(result.severity, "Warning")
        self.assertFalse(result.accepted)
        self.assertGreater(result.confidence, 0.6)  # the trap that used to bite

    def test_uncertain_keeps_accepted_false(self):
        with mock.patch.object(hazard.detector, "analyze_road",
                               return_value=_verdict(
                                   "uncertain", "Potholes", 0.514, False, [3, 4, 5, 6])):
            result = RoadDamageModel().detect("x.jpg")
        self.assertEqual(result.damage_class, "Potholes")
        self.assertEqual(result.severity, "Critical")
        self.assertFalse(result.accepted)

    def test_normal_result_has_no_damage_class_or_severity(self):
        with mock.patch.object(hazard.detector, "analyze_road", return_value=NORMAL):
            result = RoadDamageModel().detect("x.jpg")
        self.assertFalse(result.detected)
        self.assertIsNone(result.damage_class)
        self.assertIsNone(result.severity)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.to_dict()["severity"])

    def test_new_fields_are_in_to_dict(self):
        with mock.patch.object(hazard.detector, "analyze_road",
                               return_value=_verdict("damaged", "Potholes", 0.9, True, [1, 2, 3, 4])):
            d = RoadDamageModel().detect("x.jpg").to_dict()
        self.assertEqual(d["damage_class"], "Potholes")
        self.assertEqual(d["severity"], "Critical")
        self.assertTrue(d["accepted"])

    def test_detect_runs_exactly_one_inference_and_never_run_detection(self):
        """Phase 2 must not add a second inference pass, and must NOT
        reconstruct analyze_road() from run_detection() (they disagree: for
        damaged_unclassified, analyze_road returns a bbox while run_detection
        returns [])."""
        raw = [{"class_name": "Potholes", "confidence": 0.9, "box": [1, 2, 3, 4]}]
        with mock.patch.object(hazard.detector, "_raw_predict",
                               return_value=raw) as raw_predict, \
             mock.patch.object(hazard.detector, "run_detection") as run_det:
            result = RoadDamageModel().detect("x.jpg")
        self.assertEqual(raw_predict.call_count, 1)
        run_det.assert_not_called()
        self.assertEqual(result.damage_class, "Potholes")
        self.assertEqual(result.hazard_type, "pothole")
        self.assertEqual(result.severity, "Critical")
        self.assertTrue(result.accepted)

    def test_existing_constructions_still_work_without_new_fields(self):
        """Backward compatibility: HazardModel implementations that do not know
        about the new fields keep working (all fields are defaulted)."""
        result = DetectionResult(detected=True, hazard_category="waterlogging",
                                 hazard_type="waterlogged_road", confidence=0.93)
        self.assertIsNone(result.damage_class)
        self.assertIsNone(result.severity)
        self.assertIsNone(result.accepted)

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


# ---------------------------------------------------------------------------
# Acceptance threshold: 50% (lowered from 60%).
#
# These drive the REAL detector acceptance logic -- detector._raw_predict is
# mocked, but analyze_road() and its `confidence >= ACCEPT_THRESHOLD` test run
# for real -- so the boundary is asserted at the canonical seam rather than at a
# re-implementation of it. No network, no container, no YOLO inference.
# ---------------------------------------------------------------------------
class AcceptanceThresholdBoundaryTest(unittest.TestCase):

    def setUp(self):
        self.addCleanup(_reload_hazard)
        h = _local()
        self.hazard = h
        self.model = h.RoadDamageModel()
        self.svc = h.DetectionService()
        self.svc.register("road_damage", self.model, min_conf=h.DEFAULT_MIN_CONF)

    def _verdict_at(self, confidence):
        """Real analyze_road() verdict for one class at `confidence`."""
        raw = [{"class_name": "Potholes", "confidence": confidence, "box": [1, 2, 3, 4]}]
        with mock.patch.object(self.hazard.detector, "_raw_predict", return_value=raw):
            return self.model.detect("x.jpg")

    # -- the canonical numbers ---------------------------------------------

    def test_acceptance_threshold_is_50_percent(self):
        self.assertAlmostEqual(self.hazard.detector.ACCEPT_THRESHOLD, 0.50)

    def test_detection_threshold_is_unchanged_at_35_percent(self):
        """Lowering acceptance must not drag the detection floor with it."""
        self.assertAlmostEqual(self.hazard.detector.CONFIDENCE_THRESHOLD, 0.35)
        self.assertAlmostEqual(self.hazard.DEFAULT_MIN_CONF, 0.35)

    # -- the boundary, decided by the detector ------------------------------

    def test_below_50_percent_is_not_accepted_and_not_queued(self):
        for confidence in (0.49, 0.499):
            with self.subTest(confidence=confidence):
                r = self._verdict_at(confidence)
                self.assertFalse(r.accepted,
                                 "%s must not be accepted" % confidence)
                self.assertFalse(self.svc.eligible_for_queue(r),
                                 "%s must not enter the Admin queue" % confidence)

    def test_at_50_percent_is_accepted_and_queued(self):
        r = self._verdict_at(0.50)
        self.assertTrue(r.accepted)
        self.assertTrue(r.damage_class, "a named type is still reported")
        self.assertTrue(self.svc.eligible_for_queue(r))

    def test_just_above_50_percent_is_accepted_and_queued(self):
        r = self._verdict_at(0.501)
        self.assertTrue(r.accepted)
        self.assertTrue(self.svc.eligible_for_queue(r))

    def test_60_percent_is_still_accepted(self):
        """The old bar used to be the floor; 60% must not regress."""
        r = self._verdict_at(0.60)
        self.assertTrue(r.accepted)
        self.assertTrue(self.svc.eligible_for_queue(r))

    def test_confidence_between_35_and_50_still_detects_but_is_not_queued(self):
        """35%..50% is the 'uncertain' band: named, but blocked from the queue."""
        r = self._verdict_at(0.40)
        self.assertTrue(r.detected)
        self.assertTrue(self.svc.validate(r), "clears the 35% detection floor")
        self.assertFalse(r.accepted)
        self.assertFalse(self.svc.eligible_for_queue(r))

    # -- the detection floor is unchanged ----------------------------------

    def test_35_percent_detection_floor_is_independent_of_acceptance(self):
        """validate() stays the 35% floor even when acceptance is granted."""
        result = DetectionResult(detected=True, hazard_category="road_damage",
                                 hazard_type="pothole", confidence=0.35,
                                 accepted=True)
        self.assertTrue(self.svc.validate(result))
        self.assertTrue(self.svc.eligible_for_queue(result))

    def test_below_35_percent_is_not_detected_at_all(self):
        for confidence in (0.34, 0.30, 0.0):
            with self.subTest(confidence=confidence):
                result = DetectionResult(detected=True, hazard_category="road_damage",
                                         hazard_type="pothole", confidence=confidence,
                                         accepted=True)
                self.assertFalse(self.svc.validate(result))
                self.assertFalse(self.svc.eligible_for_queue(result))

    # -- unclassified tier keeps flowing to admins --------------------------

    def test_unclassified_tier_is_still_queued_though_never_accepted(self):
        """accepted is False by design for this tier, but it must keep reaching
        the Admin queue (README: surfaced and queued, flagged 'AI: uncertain')."""
        result = DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type=hazard_types.UNCLASSIFIED_SLUG, confidence=0.673,
            damage_class=hazard_types.UNCLASSIFIED_LABEL,
            severity="Warning", accepted=False)
        self.assertFalse(result.accepted)
        self.assertTrue(self.svc.eligible_for_queue(result))

    def test_named_class_below_50_is_blocked_but_unclassified_is_not(self):
        """Same accepted=False, opposite outcome: only the tier differs."""
        named = DetectionResult(detected=True, hazard_category="road_damage",
                                hazard_type="pothole", confidence=0.49,
                                damage_class="Potholes", severity="Critical",
                                accepted=False)
        unclassified = DetectionResult(
            detected=True, hazard_category="road_damage",
            hazard_type=hazard_types.UNCLASSIFIED_SLUG, confidence=0.49,
            damage_class=hazard_types.UNCLASSIFIED_LABEL, severity="Warning",
            accepted=False)
        self.assertFalse(self.svc.eligible_for_queue(named))
        self.assertTrue(self.svc.eligible_for_queue(unclassified))


# ---------------------------------------------------------------------------
# Remote inference mode (Phase 4)
#
# The ML transport is mocked at the requests boundary, so these run with no
# network, no container and no YOLO inference. Reloading hazard.py in remote
# mode is also what proves the module never imports detector there.
# ---------------------------------------------------------------------------
import importlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

import requests  # noqa: E402

_ML_ENV_VARS = ("ROAD_DAMAGE_INFERENCE_MODE", "ROAD_DAMAGE_ML_URL",
                "ROAD_DAMAGE_ML_TIMEOUT", "ROAD_DAMAGE_ML_RETRIES")

# Module state captured before any remote-mode reload, so the test can assert
# that heavy dependencies were never pulled in.
_MODULES_BEFORE_REMOTE = dict(sys.modules)

_SERVICE_RESULT = {
    "road_condition": "damaged",
    "damage_type": "Alligator Crack",
    "confidence": 0.871,
    "accepted": True,
    "bbox": [0.0, 6.7, 385.0, 516.0],
    "severity": "Critical",
}


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json object could be decoded")
        return self._body


def _reload_with(env):
    """Reload hazard.py under `env`, then restore the previous environment.

    Clearing all four ML vars before reloading is what makes the requested mode
    deterministic: nothing (including a .env file loaded earlier by app.py's
    load_dotenv, which mutates os.environ process-wide) can leak in.
    """
    saved = {k: os.environ.get(k) for k in _ML_ENV_VARS}
    for k in _ML_ENV_VARS:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return _reload_hazard()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Names pulled in by `from hazard import ...` at the top of this file. Every
# importlib.reload(hazard) re-executes the module and therefore creates NEW
# class objects, which would leave these references pointing at classes the
# module no longer uses. assertRaises(UnsupportedHazardCategory) would then
# miss the exception hazard.py actually raises. Re-binding them after each
# reload keeps the imported names and the live module in agreement.
_HAZARD_EXPORTS = ("DetectionResult", "DetectionService", "ModelInferenceError",
                   "RoadDamageModel", "UnsupportedHazardCategory")


def _reload_hazard():
    module = importlib.reload(hazard)
    for _name in _HAZARD_EXPORTS:
        globals()[_name] = getattr(module, _name)
    return module


def _local():
    """hazard.py loaded in local mode, whatever the ambient environment says."""
    h = _reload_with({})
    assert hasattr(h, "detector"), "local mode must provide the detector module"
    return h


def _png_file():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
    return path


class RemoteInferenceContractTest(unittest.TestCase):
    """Remote transport contract. No ML service, no network, no inference."""

    def setUp(self):
        # Every test leaves hazard.py back in its default local mode.
        self.addCleanup(_reload_hazard)
        self.img = _png_file()
        self.addCleanup(os.remove, self.img)

    def _remote(self, **env):
        h = _reload_with({"ROAD_DAMAGE_INFERENCE_MODE": "remote", **env})
        return h, h.RoadDamageModel()

    def _ok(self, result=None):
        return _FakeResponse(200, {"ok": True,
                                   "result": dict(_SERVICE_RESULT if result is None else result)})

    # -- configuration ----------------------------------------------------

    def test_local_is_the_default_when_env_is_unset(self):
        # Clear all four ML vars, not just the mode, so a .env loaded earlier
        # by app.py's load_dotenv cannot leave a URL/timeout behind and make
        # this pass or fail depending on the machine.
        saved = {k: os.environ.pop(k, None) for k in _ML_ENV_VARS}
        try:
            h = _reload_hazard()
            self.assertEqual(h.INFERENCE_MODE, "local")
            self.assertTrue(hasattr(h, "detector"), "local mode needs detector")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            _reload_hazard()

    def test_remote_mode_does_not_import_detector_or_ultralytics(self):
        """The dependency boundary: Flask carries no YOLO stack in remote mode."""
        for mod in ("detector", "ultralytics", "torch", "cv2"):
            sys.modules.pop(mod, None)
        try:
            h = _reload_with({"ROAD_DAMAGE_INFERENCE_MODE": "remote"})
            self.assertFalse(hasattr(h, "detector"),
                             "hazard must not import detector in remote mode")
            for mod in ("detector", "ultralytics", "torch", "cv2"):
                self.assertNotIn(mod, sys.modules,
                                 "%s must not be imported in remote mode" % mod)
        finally:
            sys.modules.update(_MODULES_BEFORE_REMOTE)

    def test_url_timeout_and_retries_are_configurable(self):
        h = _reload_with({"ROAD_DAMAGE_INFERENCE_MODE": "remote",
                          "ROAD_DAMAGE_ML_URL": "http://ml:8000/",
                          "ROAD_DAMAGE_ML_TIMEOUT": "3",
                          "ROAD_DAMAGE_ML_RETRIES": "2"})
        self.assertEqual(h.ML_URL, "http://ml:8000", "trailing slash trimmed")
        self.assertEqual(h.ML_TIMEOUT, 3.0)
        self.assertEqual(h.ML_RETRIES, 2)

    # -- happy path -------------------------------------------------------

    def test_remote_calls_service_exactly_once(self):
        h, model = self._remote()
        with mock.patch.object(h.requests, "post", return_value=self._ok()) as post:
            model.detect(self.img)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args[0][0], h.ML_URL + "/predict")
        self.assertEqual(post.call_args[1]["timeout"], h.ML_TIMEOUT)

    def test_remote_maps_damaged_into_detection_result(self):
        h, model = self._remote()
        with mock.patch.object(h.requests, "post", return_value=self._ok()):
            r = model.detect(self.img)
        self.assertTrue(r.detected)
        self.assertEqual(r.damage_class, "Alligator Crack")
        self.assertEqual(r.hazard_type, hazard_types.type_slug("Alligator Crack"))
        self.assertEqual(r.confidence, 0.871)
        self.assertEqual(r.severity, "Critical")
        self.assertTrue(r.accepted)
        self.assertEqual(r.bbox, [0.0, 6.7, 385.0, 516.0])
        self.assertEqual(r.source, "remote")
        # Model identity unchanged, so persisted rows stay comparable.
        self.assertEqual(r.model, "road_damage_yolov8s")
        self.assertEqual(r.model_version, "v1")

    def test_remote_maps_normal_to_not_detected(self):
        h, model = self._remote()
        normal = dict(road_condition="normal", damage_type=None, confidence=None,
                      accepted=False, bbox=None, severity=None)
        with mock.patch.object(h.requests, "post", return_value=self._ok(normal)):
            r = model.detect(self.img)
        self.assertFalse(r.detected)
        self.assertIsNone(r.damage_class)
        self.assertIsNone(r.severity)
        self.assertIsNone(r.bbox)
        self.assertFalse(r.accepted)

    def test_remote_unclassified_is_not_self_accepted(self):
        """Regression guard: combined-evidence confidence must never be
        re-compared against the accept threshold here."""
        h, model = self._remote()
        payload = dict(road_condition="damaged_unclassified", damage_type=None,
                       confidence=0.673, accepted=False,
                       bbox=[0.0, 230.0, 423.6, 610.2], severity="Warning")
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            r = model.detect(self.img)
        self.assertEqual(r.damage_class, "Unclassified damage")
        self.assertTrue(r.detected)
        self.assertFalse(r.accepted, "0.673 must not be re-accepted locally")
        self.assertEqual(r.severity, "Warning")

    def test_unclassified_severity_falls_back_to_warning_in_remote_mode(self):
        """Regression guard for a real drift caught in the shadow run.

        ml/server.py must resolve severity with the same
        `damage_type or "Unclassified damage"` fallback hazard.py uses. Returning
        None for the unclassified tier made remote disagree with local ('None' vs
        'Warning') on every damaged_unclassified case.
        """
        payload = dict(road_condition="damaged_unclassified", damage_type=None,
                       confidence=0.673, accepted=False,
                       bbox=[0.0, 230.0, 423.6, 610.2], severity="Warning")
        h, model = self._remote()
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            r = model.detect(self.img)
        self.assertEqual(r.damage_class, "Unclassified damage")
        self.assertEqual(r.severity, "Warning")

    def test_detected_result_with_null_severity_is_an_error(self):
        """A detected verdict with severity=null is unusable, not a default.

        Guards the remote fallback in _severity_for(): it must raise rather than
        quietly invent a severity, and must never reach for a local detector.
        """
        payload = dict(_SERVICE_RESULT, severity=None)
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=0)
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            with self.assertRaises(h.RemoteServiceError) as ctx:
                model.detect(self.img)
        self.assertIn("severity", str(ctx.exception))

    def test_severity_is_taken_from_the_response(self):
        """Severity is transported, never recomputed in remote mode."""
        h, model = self._remote()
        payload = dict(_SERVICE_RESULT, damage_type="Potholes", severity="Critical")
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            r = model.detect(self.img)
        self.assertEqual(r.severity, "Critical")
        self.assertEqual(r.damage_class, "Potholes")
        self.assertFalse(hasattr(h, "detector"))

    def test_bbox_is_exposed_in_to_dict(self):
        h, model = self._remote()
        with mock.patch.object(h.requests, "post", return_value=self._ok()):
            d = model.detect(self.img).to_dict()
        self.assertEqual(d["bbox"], [0.0, 6.7, 385.0, 516.0])
        self.assertEqual(d["source"], "remote")

    def test_local_and_remote_map_the_same_verdict_identically(self):
        """One mapping function: identical verdicts, identical DetectionResult.

        Both sides feed the SAME verdict into the mapping: the remote side
        through a mocked HTTP response, the local side through a mocked
        detector.analyze_road. So this asserts the transport-independent half of
        the contract and needs neither Docker nor a real YOLO pass.
        """
        remote_h, remote_model = self._remote()
        with mock.patch.object(remote_h.requests, "post", return_value=self._ok()):
            remote = remote_model.detect(self.img)

        # Mode is established explicitly rather than assumed: a bare
        # importlib.reload() here would inherit the ambient environment and, in a
        # remote-configured .env, silently run a real HTTP call to the ML
        # service instead of the local path under test.
        local_h = _local()
        with mock.patch.object(local_h.detector, "analyze_road",
                               return_value=dict(_SERVICE_RESULT)):
            local = local_h.RoadDamageModel().detect(self.img)

        for field in ("detected", "hazard_category", "hazard_type", "confidence",
                      "damage_class", "severity", "accepted", "bbox", "model",
                      "model_version"):
            self.assertEqual(getattr(remote, field), getattr(local, field),
                             "field %r differs between modes" % field)
        self.assertEqual(local.source, "local")
        self.assertEqual(remote.source, "remote")

    # -- retry behaviour --------------------------------------------------

    def test_timeout_retries_once_then_raises(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        with mock.patch.object(h.requests, "post",
                               side_effect=requests.Timeout("timed out")) as post, \
             mock.patch.object(h.time, "sleep") as sleep:
            with self.assertRaises(h.RemoteServiceError):
                model.detect(self.img)
        self.assertEqual(post.call_count, 2, "initial attempt + exactly one retry")
        sleep.assert_called_once_with(0.5)

    def test_connection_error_retries_once(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        with mock.patch.object(h.requests, "post",
                               side_effect=requests.ConnectionError("refused")) as post:
            with self.assertRaises(h.RemoteServiceError):
                model.detect(self.img)
        self.assertEqual(post.call_count, 2)

    def test_503_is_retried_once(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        warming = _FakeResponse(503, {"ok": False, "error": {"reason": "model_not_loaded"}})
        with mock.patch.object(h.requests, "post", side_effect=[warming, warming]) as post, \
             mock.patch.object(h.time, "sleep"):
            with self.assertRaises(h.RemoteServiceError):
                model.detect(self.img)
        self.assertEqual(post.call_count, 2)

    def test_503_recovers_on_retry(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        warming = _FakeResponse(503, {"ok": False, "error": {"reason": "model_not_loaded"}})
        with mock.patch.object(h.requests, "post", side_effect=[warming, self._ok()]) as post, \
             mock.patch.object(h.time, "sleep"):
            r = model.detect(self.img)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(r.detected)

    def test_500_inference_failed_is_not_retried(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        failed = _FakeResponse(500, {"ok": False, "error": {"reason": "inference_failed"}})
        with mock.patch.object(h.requests, "post", return_value=failed) as post:
            with self.assertRaises(h.RemoteServiceError) as ctx:
                model.detect(self.img)
        self.assertEqual(post.call_count, 1, "inference already ran and failed")
        self.assertIn("inference_failed", str(ctx.exception))

    def test_4xx_is_not_retried(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        for status in (400, 413):
            resp = _FakeResponse(status, {"ok": False, "error": {"reason": "bad_request"}})
            with mock.patch.object(h.requests, "post", return_value=resp) as post:
                with self.assertRaises(h.RemoteServiceError):
                    model.detect(self.img)
            self.assertEqual(post.call_count, 1, "HTTP %d must not retry" % status)

    def test_502_and_504_are_not_retried(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=1)
        for status in (502, 504):
            with mock.patch.object(h.requests, "post",
                                   return_value=_FakeResponse(status, {"ok": False})) as post:
                with self.assertRaises(h.RemoteServiceError):
                    model.detect(self.img)
            self.assertEqual(post.call_count, 1, "HTTP %d must not retry" % status)

    def test_no_retries_when_disabled(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=0)
        with mock.patch.object(h.requests, "post",
                               side_effect=requests.ConnectionError("refused")) as post:
            with self.assertRaises(h.RemoteServiceError):
                model.detect(self.img)
        self.assertEqual(post.call_count, 1)

    # -- malformed / hostile responses ------------------------------------

    def test_malformed_responses_raise_instead_of_returning_normal(self):
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=0)
        cases = {
            "non-JSON body": _FakeResponse(200, None),
            "ok is false": _FakeResponse(200, {"ok": False, "result": _SERVICE_RESULT}),
            "no result key": _FakeResponse(200, {"ok": True}),
            "result not an object": _FakeResponse(200, {"ok": True, "result": "damaged"}),
            "missing severity": _FakeResponse(
                200, {"ok": True, "result": {k: v for k, v in _SERVICE_RESULT.items()
                                             if k != "severity"}}),
            "missing road_condition": _FakeResponse(
                200, {"ok": True, "result": {k: v for k, v in _SERVICE_RESULT.items()
                                             if k != "road_condition"}}),
        }
        for label, resp in cases.items():
            with self.subTest(label):
                with mock.patch.object(h.requests, "post", return_value=resp):
                    with self.assertRaises(h.RemoteServiceError):
                        model.detect(self.img)

    def test_service_failure_surfaces_as_model_inference_error(self):
        """The pre-existing soft-fail path, not a fake 'nothing detected'."""
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=0)
        with mock.patch.object(h.requests, "post",
                               side_effect=requests.ConnectionError("down")):
            # Use the reloaded module's classes: reload() rebinds them, so the
            # module-level import captured at file load is a different object.
            svc = h.DetectionService()
            svc.register("road_damage", model, min_conf=0.35)
            with self.assertRaises(h.ModelInferenceError):
                svc.detect("road_damage", self.img)

    def test_never_falls_back_to_local_inference(self):
        """A remote outage must not quietly run the local detector."""
        h, model = self._remote(ROAD_DAMAGE_ML_RETRIES=0)
        # A local detector instance to spy on. It is NOT reachable from the
        # remote-mode module (that is the point), so patch the real module
        # object directly rather than the remote-mode one.
        local_detector = importlib.import_module("detector")
        with mock.patch.object(h.requests, "post",
                               side_effect=requests.ConnectionError("down")), \
             mock.patch.object(local_detector, "analyze_road") as local_detect:
            with self.assertRaises(h.RemoteServiceError):
                model.detect(self.img)
            local_detect.assert_not_called()

    # -- the same 50% bar, reached over the transport -----------------------
    # The service computes `accepted` with the same detector.ACCEPT_THRESHOLD,
    # so remote mode must honour that transported verdict for queue entry. The
    # HTTP call is mocked: no container required.

    def test_remote_uses_the_transported_acceptance_verdict_for_queue_entry(self):
        h, model = self._remote()
        cases = [
            # (confidence, accepted the service reports, expect queued)
            (0.49, False, False),
            (0.499, False, False),
            (0.50, True, True),
            (0.501, True, True),
            (0.60, True, True),
        ]
        svc = h.DetectionService()
        svc.register("road_damage", model, min_conf=0.35)
        for confidence, accepted, expect_queued in cases:
            with self.subTest(confidence=confidence):
                payload = dict(_SERVICE_RESULT, confidence=confidence,
                               accepted=accepted)
                with mock.patch.object(h.requests, "post",
                                       return_value=self._ok(payload)):
                    r = model.detect(self.img)
                self.assertEqual(r.accepted, accepted)
                self.assertEqual(svc.eligible_for_queue(r), expect_queued)

    def test_remote_unclassified_tier_still_queues(self):
        """Remote unclassified is never accepted by the service, yet still
        reaches the Admin queue, exactly as it does in local mode."""
        h, model = self._remote()
        payload = dict(_SERVICE_RESULT, road_condition="damaged_unclassified",
                       damage_type=None, confidence=0.673, accepted=False,
                       severity="Warning")
        svc = h.DetectionService()
        svc.register("road_damage", model, min_conf=0.35)
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            r = model.detect(self.img)
        self.assertFalse(r.accepted)
        self.assertEqual(r.damage_class, hazard_types.UNCLASSIFIED_LABEL)
        self.assertTrue(svc.eligible_for_queue(r))

    def test_remote_detection_floor_still_35_percent(self):
        h, model = self._remote()
        svc = h.DetectionService()
        svc.register("road_damage", model, min_conf=0.35)
        # 30% but accepted: below the 35% detection floor, so not queued even
        # though the service accepted it. The detection floor is not the thing
        # that moved.
        payload = dict(_SERVICE_RESULT, confidence=0.30, accepted=True)
        with mock.patch.object(h.requests, "post", return_value=self._ok(payload)):
            r = model.detect(self.img)
        self.assertFalse(svc.validate(r))
        self.assertFalse(svc.eligible_for_queue(r))


if __name__ == "__main__":
    unittest.main()
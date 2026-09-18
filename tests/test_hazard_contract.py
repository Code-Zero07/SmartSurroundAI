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
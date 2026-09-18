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
"""hazard.py
-----------
Generic detection layer. The reporting workflow consumes ONLY the normalized
DetectionResult contract — it never knows about YOLO classes, per-model
confidence semantics, or detector internals.

A future model (e.g. waterlogging) implements the HazardModel protocol and is
registered on a DetectionService under its hazard_category; nothing else in
the pipeline changes.

This module is the seam between the Flask app and the standalone ML service.
`RoadDamageModel.detect()` has two modes, selected by
ROAD_DAMAGE_INFERENCE_MODE:

  local  (default)  call detector.analyze_road() in-process.
  remote           POST the image to the ML service's /predict endpoint.

Both modes feed the SAME mapping function, so the normalized contract cannot
diverge between them. Neither mode re-derives a detector decision: thresholds,
the fallback/noisy-OR tier, severity and `accepted` are all produced by
detector.py and merely carried across. See README.md for the env surface.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import hazard_types
import requests

# ---------------------------------------------------------------------------
# Inference transport configuration
# ---------------------------------------------------------------------------
# Read once at import so behaviour is stable for the process lifetime (and so
# tests can control it deterministically). Default is deliberately `local`:
# the remote path is opt-in until the shadow parity run is signed off.
INFERENCE_MODE = os.environ.get("ROAD_DAMAGE_INFERENCE_MODE", "local").strip().lower()
ML_URL = os.environ.get("ROAD_DAMAGE_ML_URL", "http://127.0.0.1:8000").rstrip("/")
ML_TIMEOUT = float(os.environ.get("ROAD_DAMAGE_ML_TIMEOUT", "15"))
ML_RETRIES = int(os.environ.get("ROAD_DAMAGE_ML_RETRIES", "1"))
_ML_RETRY_BACKOFF_SECONDS = 0.5

# `detector` pulls in Ultralytics/PyTorch at import time (detector.py does
# `from ultralytics import YOLO` at module scope). In remote mode the whole
# point is that the Flask process does NOT carry that stack, so the import is
# conditional. In local mode severity comes from detector.severity_for();
# in remote mode it arrives in the service response instead.
if INFERENCE_MODE == "local":
    import detector
else:
    # Remote mode must leave no reference behind. importlib.reload() reuses the
    # existing module dict, so a `detector` name set by an earlier local-mode
    # import would otherwise survive into a remote-mode reload. Drop it so the
    # boundary holds on reload, not just on a fresh interpreter.
    globals().pop("detector", None)


@dataclass
class DetectionResult:
    detected: bool
    hazard_category: str
    hazard_type: Optional[str] = None
    confidence: Optional[float] = None
    model: str = ""
    model_version: Optional[str] = None
    # `damage_class` is the detector's OWN class label (display name, e.g.
    # 'Potholes') as opposed to `hazard_type`, the normalized slug. Both
    # detector.severity_for() and hazard_types.type_slug() key on the display
    # name, so it has to survive normalization or severity silently degrades
    # to DEFAULT_SEVERITY. `accepted` is the detector's own accept decision and
    # must NOT be re-derived from `confidence`: for the "damaged_unclassified"
    # tier `confidence` is a combined-evidence score, not a class confidence,
    # so `confidence >= ACCEPT_THRESHOLD` would self-accept a case the detector
    # deliberately refused to accept.
    damage_class: Optional[str] = None
    severity: Optional[str] = None
    accepted: Optional[bool] = None
    # `bbox` is the detector's xyxy box, transported and kept for parity
    # auditing. Nothing persists or renders it today; it is additive on the
    # /api/detect response only.
    bbox: Optional[list] = None
    # Where the verdict was produced: "local" (in-process detector) or
    # "remote" (ML service). Diagnostics only — not persisted, and deliberately
    # NOT folded into `model` so the persisted detection_model string stays
    # comparable across the migration.
    source: Optional[str] = None

    def to_dict(self):
        return {
            "detected": self.detected,
            "hazard_category": self.hazard_category,
            "hazard_type": self.hazard_type,
            "confidence": self.confidence,
            "model": self.model,
            "model_version": self.model_version,
            "damage_class": self.damage_class,
            "severity": self.severity,
            "accepted": self.accepted,
            "bbox": self.bbox,
            "source": self.source,
        }


class ModelInferenceError(Exception):
    """A registered model failed to produce a result (soft-fail surface)."""


class RemoteServiceError(Exception):
    """The ML service was unreachable, timed out, or answered unusably.

    Never swallowed into a "normal" verdict: callers must treat this as a
    failed inference (DetectionService.detect turns it into
    ModelInferenceError) so an outage surfaces as an error rather than as a
    silently-not-detected report.
    """


class UnsupportedHazardCategory(Exception):
    """A hazard_category with no registered model was requested."""


class HazardModel(Protocol):
    model_name: str
    model_version: Optional[str]

    def detect(self, image_path: str) -> DetectionResult:
        ...


# Keys the ML service must return. Anything missing means we are talking to
# something that is not our service, and guessing would risk inventing a
# verdict — so it is an error instead.
_REQUIRED_REMOTE_KEYS = (
    "road_condition", "damage_type", "confidence", "accepted", "bbox", "severity",
)

# Only these are worth a second attempt. Everything else fails deterministically
# and retrying would just re-run inference the service already rejected:
#   Timeout / ConnectionError -> transient transport fault, service may recover
#   503 model_not_loaded     -> service still warming up
# 500 inference_failed is NOT retried: the model already ran and failed, so a
# second call re-runs it and re-fails.
_RETRY_STATUS_CODES = (503,)


def _fetch_remote(image_path):
    """POST one image to the ML service and return (verdict, severity).

    Transport only — no detector decision is made here. Returns the service's
    own analyze_road() dict plus the severity it computed, both untouched.
    """
    url = "%s/predict" % ML_URL
    with open(image_path, "rb") as fh:
        payload = fh.read()
    files = {"image": (os.path.basename(image_path), payload)}

    attempts = ML_RETRIES + 1
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, files=files, timeout=ML_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(_ML_RETRY_BACKOFF_SECONDS)
                continue
            raise RemoteServiceError(
                "ML service unreachable at %s after %d attempt(s): %s: %s"
                % (url, attempts, type(exc).__name__, exc)) from exc

        if resp.status_code == 503 and attempt < attempts:
            # Warming up (or briefly overloaded). Back off, then try once more.
            time.sleep(_ML_RETRY_BACKOFF_SECONDS)
            continue

        if resp.status_code >= 400:
            raise RemoteServiceError(
                "ML service returned HTTP %d from %s: %s"
                % (resp.status_code, url, _error_detail(resp)))

        try:
            body = resp.json()
        except ValueError as exc:
            raise RemoteServiceError(
                "ML service returned non-JSON from %s: %s" % (url, exc)) from exc

        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RemoteServiceError(
                "ML service returned an unusable body from %s: %r" % (url, body))

        result = body.get("result")
        if not isinstance(result, dict):
            raise RemoteServiceError(
                "ML service response has no result object: %r" % (body,))

        missing = [k for k in _REQUIRED_REMOTE_KEYS if k not in result]
        if missing:
            raise RemoteServiceError(
                "ML service response missing key(s): %s" % ", ".join(missing))

        return result, result["severity"]

    # Unreachable: the loop either returns or raises on its final attempt.
    raise RemoteServiceError(
        "ML service at %s failed after %d attempt(s): %s" % (url, attempts, last_error))


def _error_detail(resp):
    """Best-effort extraction of the service's structured error reason."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return "%s: %s" % (err.get("reason"), err.get("detail"))
        return str(err or body)[:200]
    return str(body)[:200]


def _severity_for(damage_class, severity_from_service):
    """Resolve severity without ever touching `detector` in remote mode.

    Local mode computes it from the detector's own table. Remote mode receives
    it from the service, which called that same function. The fallback is
    guarded rather than bare `detector.severity_for(...)` so a missing
    detector module in remote mode raises a clear error instead of a NameError.
    """
    if severity_from_service is not None:
        return severity_from_service
    if INFERENCE_MODE != "local":
        raise RemoteServiceError(
            "ML service returned severity=null for a detected road condition")
    return detector.severity_for(damage_class)


def _verdict_to_result(result, severity=None, source="local"):
    """Map a detector verdict onto DetectionResult. Identical in both modes.

    `result` is detector.analyze_road()'s dict, either obtained locally or
    carried back from the ML service untouched. Nothing here re-derives a
    detector decision: severity and `accepted` are taken as given.
    """
    if result["road_condition"] == "normal":
        return DetectionResult(
            detected=False, hazard_category="road_damage",
            hazard_type=None, confidence=None,
            damage_class=None, severity=None, accepted=False,
            bbox=result.get("bbox"),
            model=RoadDamageModel.model_name,
            model_version=RoadDamageModel.model_version,
            source=source,
        )
    # Capture the detector's display name BEFORE slugging: severity_for()
    # and type_slug() both key on it, and only the slug is kept below.
    damage_class = result["damage_type"] or hazard_types.UNCLASSIFIED_LABEL
    return DetectionResult(
        detected=True,
        hazard_category="road_damage",
        hazard_type=hazard_types.type_slug(damage_class),
        confidence=result["confidence"],
        damage_class=damage_class,
        severity=_severity_for(damage_class, severity),
        # Straight from the detector. Do not recompute from confidence:
        # the unclassified tier's confidence is combined evidence.
        accepted=result["accepted"],
        bbox=result.get("bbox"),
        model=RoadDamageModel.model_name,
        model_version=RoadDamageModel.model_version,
        source=source,
    )


class RoadDamageModel:
    """Adapter over detector.analyze_road(), local or via the ML service.

    detector.py stays the only place that knows YOLO classes, thresholds,
    the fallback tier and severity. In remote mode this class never imports
    it at all, so the Flask process carries no YOLO/Ultralytics/PyTorch."""

    model_name = "road_damage_yolov8s"
    model_version = "v1"

    def detect(self, image_path):
        if INFERENCE_MODE == "remote":
            result, severity = _fetch_remote(image_path)
            return _verdict_to_result(result, severity=severity, source="remote")
        return _verdict_to_result(detector.analyze_road(image_path))


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
            # A RemoteServiceError (and any other transport/validation failure)
            # lands here too, so an ML outage surfaces through the pre-existing
            # ModelInferenceError path the routes already handle. It is never
            # downgraded to a "normal" verdict.
            raise ModelInferenceError(category) from exc

    def min_confidence(self, category):
        return self._min_conf.get(category)

    def validate(self, result):
        """Detection threshold only (ROAD_DAMAGE_MIN_CONF, 0.35).

        This is "did we detect anything worth naming", not "do we trust it".
        Unchanged semantics: callers that gate Admin-queue entry must use
        eligible_for_queue() instead.
        """
        threshold = self.min_confidence(result.hazard_category)
        if threshold is None:
            return False
        return (
            result.detected
            and result.confidence is not None
            and result.confidence >= threshold
        )

    def eligible_for_queue(self, result):
        """Admin-queue entry: detection threshold AND the detector's acceptance.

        validate() covers the 35% detection floor. This adds the detector's own
        acceptance decision, which for a named class is exactly
        `confidence >= detector.ACCEPT_THRESHOLD` (50%). `accepted` is computed
        by detector.py in local mode and carried back verbatim from the ML
        service in remote mode, so both modes enforce the same 50% bar without
        this module restating the number.
        """
        if not self.validate(result):
            return False
        if result.accepted:
            return True
        # The unclassified tier is never auto-accepted by design (see
        # detector.analyze_road), yet it is still surfaced and queued as
        # "AI: uncertain" -- that tier's confidence is combined evidence from
        # several weak classes, not a named class's score, so the named-class
        # acceptance bar does not apply to it. It is identified by its existing
        # normalized slug rather than a second threshold.
        return result.hazard_type == hazard_types.UNCLASSIFIED_SLUG


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
"""
ml/server.py
------------
Standalone road-damage ML inference service (FastAPI + Uvicorn).

    SmartSurround Flask  --HTTP POST /predict-->  smart-surround-ml  --> YOLO

DESIGN RULE: this file is a TRANSPORT SHIM ONLY.

`detector.py` is imported and used unmodified. It remains the single source of
truth for every decision: class selection, confidence, accept threshold, the
noisy-OR unclassified fallback, severity mapping, bounding boxes and error
behaviour. This module deliberately does NOT:

  * call detector.run_detection()            (Phase 1 proved it disagrees with
                                              analyze_road(); reconstructing one
                                              from the other loses the bbox on
                                              damaged_unclassified cases)
  * recompute confidence, severity, acceptance or any class mapping
  * re-interpret, normalize or "fix" the detector's verdict
  * install packages at runtime

What it does add is exactly the boundary concerns that are not the detector's
job: HTTP framing, a temp file (because analyze_road() takes a filesystem
path), a single resident model, and a structured error envelope.

Run locally:
    python -m uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1
"""

import os

# MUST be set before `import ultralytics` (AUTOINSTALL is read at import time).
# Rationale is documented at length in requirements-ml.txt: with pi-heif absent
# and autoinstall enabled, ultralytics live-pip-installs pi-heif on the first
# undecodable image and upgrades Pillow underneath us. These two variables make
# the absence of pi-heif safe and explicit rather than accidental. They are
# also set as ENV in the Dockerfile; setting them again here means the service
# is safe even when run outside the container.
os.environ["YOLO_AUTOINSTALL"] = "false"
os.environ["ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS"] = "1"

import re  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Optional  # noqa: E402

from fastapi import FastAPI, File, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# detector.py lives at the repository root, one level up from ml/.
# Inserting it here keeps `import detector` working unchanged, so the detector
# resolves its own default model path relative to its own __file__ and
# ROAD_DAMAGE_MODEL_PATH still overrides it.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import detector  # noqa: E402

# One resident model, one worker, one inference at a time.
_inference_lock = threading.Lock()

MAX_UPLOAD_BYTES = int(os.environ.get("ML_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
_TMP_PREFIX = "ml-predict-"

# Extension allowlist. The suffix matters: the corpus is jpg/png/webp and the
# decoder is chosen from the decoded container, so dropping a legitimate
# extension could change which decoder opens the file. Anything not on this
# list gets a suffix derived from the declared content type instead.
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_CONTENT_TYPE_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _suffix_for(filename: Optional[str], content_type: Optional[str]) -> str:
    if filename:
        _, ext = os.path.splitext(filename)
        if ext and _SAFE_SUFFIX.match(ext):
            return ext.lower()
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _CONTENT_TYPE_SUFFIX:
            return _CONTENT_TYPE_SUFFIX[base]
    # Last resort. analyze_road() decodes with OpenCV, which sniffs content
    # rather than trusting the extension.
    return ".img"


def _error(reason: str, status: int, **extra) -> JSONResponse:
    """Structured non-2xx envelope: {"ok": false, "reason": "..."}."""
    body = {"ok": False, "reason": reason}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class PredictResponse(BaseModel):
    ok: bool = True
    # The detector's result, passed through verbatim. Never normalized.
    result: dict


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the model during STARTUP, not per request. detector.get_model() is a
    # module-level singleton, so this keeps exactly one copy resident for the
    # process lifetime.
    t0 = time.time()
    model = detector.get_model()
    app.state.model_load_seconds = round(time.time() - t0, 3)
    app.state.model = model
    app.state.model_path = detector.MODEL_PATH
    yield
    app.state.model = None


app = FastAPI(
    title="smart-surround-ml",
    version="1.0.0",
    description="Road-damage inference service. Wraps the unmodified detector.py.",
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz():
    """Liveness + model residency. Reports the model actually held in memory."""
    model = getattr(app.state, "model", None)
    loaded = model is not None
    body = {
        "ok": loaded,
        "status": "healthy" if loaded else "model_not_loaded",
        "model_loaded": loaded,
        "model_path": getattr(app.state, "model_path", None),
        "model_load_seconds": getattr(app.state, "model_load_seconds", None),
        "model_class": type(model).__name__ if loaded else None,
        "thresholds": {
            "confidence": detector.CONFIDENCE_THRESHOLD,
            "accept": detector.ACCEPT_THRESHOLD,
            "fallback_evidence": detector.FALLBACK_EVIDENCE_THRESHOLD,
            "raw_conf_floor": detector.RAW_CONF_FLOOR,
        },
    }
    if not loaded:
        return JSONResponse(status_code=503, content=body)
    return body


# Sync `def` on purpose: inference is blocking CPU work, so FastAPI runs this in
# its threadpool instead of stalling the event loop.
@app.post("/predict", response_model=None)
def predict(
    image: Optional[UploadFile] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
):
    """Run road-damage analysis on one uploaded image.

    Accepts the image as multipart form data under either `image` (preferred)
    or `file`. Calls detector.analyze_road() EXACTLY ONCE and returns its
    result plus the severity the detector's own table assigns. Never calls
    run_detection(). The verdict itself is returned unchanged — no threshold,
    fallback or accept decision is recomputed at the HTTP boundary.
    """
    upload = image or file
    if upload is None:
        return _error("missing_image", 400,
                      detail="send one image as multipart field 'image' (or 'file')")

    if getattr(app.state, "model", None) is None:
        return _error("model_not_loaded", 503)

    data = upload.file.read()
    if not data:
        return _error("empty_file", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        return _error("file_too_large", 413,
                      detail="max %d bytes" % MAX_UPLOAD_BYTES)

    suffix = _suffix_for(upload.filename, upload.content_type)
    tmp_path = None
    try:
        # analyze_road() takes a filesystem path, so the bytes must land on
        # disk. Same directory as TMPDIR to keep the write on one filesystem.
        fd, tmp_path = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=suffix,
                                        dir=tempfile.gettempdir())
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

        # Single inference. This is the ONLY detector decision call in the
        # service; the lock serializes it because the resident model is shared.
        with _inference_lock:
            result = detector.analyze_road(tmp_path)

        # Add severity, computed by the SAME detector.severity_for() the
        # in-process path used before the service was extracted. Remote callers
        # (Flask in remote mode) must not reimplement this table, so it travels
        # with the verdict. No other detector decision is touched.
        #
        # The "Unclassified damage" fallback is REQUIRED, not cosmetic: for the
        # damaged_unclassified tier damage_type is null, and the local path
        # resolves `result["damage_type"] or "Unclassified damage"` before
        # calling severity_for(). Returning None here instead of
        # severity_for("Unclassified damage") == "Warning" would silently make
        # remote mode disagree with local on every unclassified case.
        if result.get("road_condition") == "normal":
            severity = None
        else:
            damage_class = result.get("damage_type") or "Unclassified damage"
            severity = detector.severity_for(damage_class)
        return {"ok": True, "result": {**result, "severity": severity}}
    except Exception as exc:
        # Structured, non-2xx. The detector's own error semantics are not
        # reinterpreted here; the message is surfaced for diagnosis.
        return _error("inference_failed", 500,
                      detail="%s: %s" % (type(exc).__name__, exc))
    finally:
        # Runs on success, on detector failure, and on client disconnect.
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.get("/")
def root():
    return {"service": "smart-surround-ml", "endpoints": ["/healthz", "/predict"]}

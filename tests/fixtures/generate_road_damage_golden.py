"""
tests/fixtures/generate_road_damage_golden.py
---------------------------------------------
Regenerates `tests/fixtures/road_damage_golden.json` from the live `uploads/`
corpus using the CURRENT, UNMODIFIED `detector.py`.

This is a one-shot characterization/provenance tool, NOT a test. It exists so the
pre-Dockerization detector behaviour is recorded field-for-field and can be
diffed (`git diff tests/fixtures/road_damage_golden.json`) after any change to
`detector.py`, `hazard.py`, the weights, or the thresholds.

Run:  python tests/fixtures/generate_road_damage_golden.py

Deliberate design constraints
-----------------------------
  * `import detector` DIRECTLY, never via `app`. Importing `app` would trigger
    `load_dotenv()` and couple the fixture to the developer's local `.env`.
  * No timestamp is written into the JSON, so two runs of the same code against
    the same corpus produce byte-identical files and `git diff` is meaningful.
  * Nothing is normalized, rounded, reinterpreted or corrected. Whatever
    `detector.analyze_road()` / `run_detection()` return is what gets recorded.
  * The `detections` table in `smartsurround.db` is copied in as PROVENANCE
    ONLY (`db_reference`) and is never asserted against: those rows were
    written across two different normalization eras (display class names in
    ids 1-12, slugs in ids 33-40) and predate the current thresholds.
  * Inference is CPU-only and deterministic for a given image, but
    `analyze_road()` and `run_detection()` each call `_raw_predict()`, so each
    image is inferred TWICE. That is intentional: both public functions are
    recorded independently, exactly as they behave today.
  * YOLO_AUTOINSTALL is forced to "false" BEFORE ultralytics is imported.
    See the _meta.decode_notes below -- without this the generator live
    `pip install`s pi-heif into the venv, which pulls pillow>=11.1.0 and
    silently upgrades the Pillow==10.4.0 pin in requirements.txt.
"""

import hashlib
import json
import os
import sys

os.environ["YOLO_AUTOINSTALL"] = "false"   # must precede `import ultralytics`

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

UPLOADS = os.path.join(ROOT, "uploads")
OUT_PATH = os.path.join(HERE, "road_damage_golden.json")

# ---------------------------------------------------------------------------
# The golden set. 20 UNIQUE readable images (deduped by MD5 across 57 files in
# uploads/, which contain only 38 distinct images) chosen for spread across:
#   * all 4 damage classes + the "Unclassified damage" fallback
#   * all 4 road_condition verdicts (damaged / uncertain / damaged_unclassified
#     / normal)
#   * confidences from 0.374 up to 0.871
#   * formats jpg / png / webp
#   * portrait, landscape and near-square dimensions
#   * 37 KB .. 1.2 MB
# `db_reference` is the historical detections row for that image (None = the
# image has never been inferred through the app). PROVENANCE ONLY.
# ---------------------------------------------------------------------------
MANIFEST = [
    # --- "damaged", confidence >= ACCEPT_THRESHOLD (0.60) --------------------
    ("cb6256f85682428682a293a6ab7cac64.jpg",  4, "Alligator Crack",  0.871,
     "highest confidence on record"),
    ("4afd7fe02116464f9df0f6aefcea1898.jpg",  7, "Potholes",         0.813,
     "Potholes, high confidence"),
    ("81e2bfc5b98b43ec8c48c23a619e69d2.png", 12, "Longitudinal Crack", 0.823,
     "the 15-copy duplicate group; rows 30/31 for it were admin-rejected"),
    ("243a821989c24074b7ab91cc640f8458.jpg",  5, "Potholes",         0.733,
     "Potholes, mid confidence"),
    ("06eb20625cdf4987b1094ca7da6cec17.jpg",  6, "Potholes",         0.716,
     "Potholes, lower confidence"),

    # --- ACCEPT_THRESHOLD boundary (0.609 / 0.634 straddle 0.60) ------------
    ("03574fd4f48a4d389759b5ae03450a51.png", 11, "Alligator Crack",  0.609,
     "boundary: 0.609 vs ACCEPT_THRESHOLD 0.60"),
    ("2af17ada35c547e3980c21264e8d6c1a.jpg", 37, "alligator_crack",  0.634,
     "slug-era row; expect the display class name back"),

    # --- "damaged_unclassified" (noisy-OR fallback) --------------------------
    ("3d59936fc8df449bad634195236d143a.png",  3, "Unclassified damage", 0.673,
     "unclassified with HIGH combined evidence, ai_accepted=0"),
    ("4f4e0055af194057bcb91bd0e4eebd69.jpg", 40, "pothole",          0.673,
     "slug-era row; live evidence that app.py:437 mislabels severity"),
    ("b97653478136489dbf8e503e01213422.jpg",  2, "Unclassified damage", 0.596,
     "unclassified, near the fallback edge"),
    ("4bf2f409c6c449129dfeb91324c4f998.webp",  1, "Unclassified damage", 0.481,
     "the only WEBP; rows 14/15 for it were admin-rejected"),

    # --- "uncertain" band (0.35 .. 0.60) ------------------------------------
    ("6b6655601f814ad5b7163a9312aa3e0c.jpg",  8, "Potholes",          0.514,
     "uncertain band"),
    ("21000c23d7f84d2cb4edf6517b83cc4c.png", 10, "Alligator Crack",   0.503,
     "uncertain band, non-640 source resolution"),
    ("3d8abdb12dcf41fbb4e3500e59a97517.jpg",  9, "Potholes",          0.393,
     "uncertain band, low end"),
    ("07b261f8b9214131bafd8ad297a0695c.jpg", 38, "pothole",           0.374,
     "lowest confidence on record; 0.374 vs CONFIDENCE_THRESHOLD 0.35"),

    # --- never inferred through the app: the only possible source of the
    #     "normal" verdict, which has ZERO historical coverage in the DB ------
    ("e0b907f4fbd04f1b8c0cef71e168ab15.png", None, None, None,
     "never inferred; expect 'normal'"),
    ("9f7db23927274a558c28be367dc90693.png", None, None, None,
     "never inferred; largest selected file (631 KB)"),
    ("74a7feae63584b5599932583291fcfe6.png", None, None, None,
     "never inferred; expect 'normal'"),
    ("0ec73e070ecc4e0788af3dd0494f3ddb.jpg", None, None, None,
     "never inferred; expect 'normal'"),
    ("a1da6fdcf56d448eb4dc6cdd7d246b77.jpg", None, None, None,
     "never inferred; expect 'normal'"),
]

# ---------------------------------------------------------------------------
# Negative cases: files in uploads/ that PIL cannot even open. Recorded as
# their own array so the current failure behaviour stays pinned -- this is the
# error path RoadDamageModel.detect() must keep converting into
# hazard.ModelInferenceError, and the path /api/detect maps to HTTP 422.
# ---------------------------------------------------------------------------
NEGATIVE_CASES = [
    "406ac58aa03941cd938fbeb44391dafd.png",
    "8d689315a36c46d7ab7442974eb37ddb.png",
    "6248814500764287baa0ba284909618b.png",
]


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _raw_pil_open():
    """The ORIGINAL, unpatched PIL.Image.open.

    ultralytics/utils/patches.py monkey-patches PIL.Image.open with a wrapper
    that, on ANY decode failure, swallows the original exception and tries to
    install/import pi_heif for HEIC support. That wrapper must NOT be used to
    decide whether a file is a valid image, or a corrupt upload gets reported
    as ModuleNotFoundError instead of UnidentifiedImageError.
    """
    try:
        from ultralytics.utils.patches import _image_open
        return _image_open
    except Exception:                              # pragma: no cover
        from PIL import Image
        return Image.open


def _decode(path, opener):
    try:
        with opener(path) as im:
            return {"ok": True, "size": [im.width, im.height], "format": im.format}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "error_message": str(exc)}


def _image_meta(path):
    """Local metadata only (bytes / md5 / pixel dimensions). Never fed to the
    model -- detector.py does its own decoding. Decoded with the RAW Pillow
    opener so corruption is reported as corruption."""
    meta = {"image": os.path.relpath(path, ROOT).replace("\\", "/"),
            "md5": _md5(path),
            "bytes": os.path.getsize(path)}
    dec = _decode(path, _raw_pil_open())
    if dec["ok"]:
        meta["size"] = dec["size"]
        meta["format"] = dec["format"]
    else:
        meta["size"] = None
        meta["format"] = None
        meta["decode_error"] = dec
    return meta


def build():
    import detector                                 # NOT via app: no .env
    import torch
    import ultralytics

    model_path = detector.MODEL_PATH

    fixture = {
        "_meta": {
            "purpose": ("Field-for-field characterization of the CURRENT "
                        "detector.py behaviour, captured before the ML "
                        "inference service is extracted into Docker. "
                        "Regenerate with tests/fixtures/"
                        "generate_road_damage_golden.py and review the diff."),
            "assertion_policy": ("Nothing here is normalized or corrected. "
                                 "db_reference is PROVENANCE ONLY and is never "
                                 "asserted against: those rows span two "
                                 "normalization eras (display class names in "
                                 "ids 1-12, slugs in ids 33-40) and predate "
                                 "the current thresholds."),
            "corpus_note": ("uploads/ is gitignored. This fixture is only "
                            "reproducible on a machine holding the corpus; "
                            "the per-case md5 lets a future corpus be matched "
                            "byte-for-byte."),
            "decode_notes": {
                "corrupt_uploads_do_not_raise": (
                    "detector.analyze_road() does NOT raise on an undecodable "
                    "image. OpenCV logs 'Image Read Error', ultralytics returns "
                    "an empty result, and analyze_road() reports road_condition="
                    "'normal' with confidence=null. app.py therefore takes the "
                    "svc.validate()==False path: the citizen is told 'No valid "
                    "hazard detected above the confidence threshold' (HTTP 200, "
                    "valid:false) instead of the 422 'Detection failed' path "
                    "behind hazard.ModelInferenceError. An unreadable photo is "
                    "indistinguishable from a clean road. A future ML service "
                    "MUST NOT reproduce this."),
                "pi_heif_landmine": (
                    "ultralytics/utils/patches.py:78-88 monkey-patches "
                    "PIL.Image.open so that ANY decode failure triggers "
                    "check_requirements('pi-heif') and then `from pi_heif "
                    "import register_heif_opener`. With pi-heif absent and "
                    "YOLO_AUTOINSTALL unset, that live-pip-installs pi-heif "
                    "into the running environment, which pulls pillow>=11.1.0 "
                    "and upgrades the Pillow==10.4.0 pin. Offline or read-only, "
                    "the original UnidentifiedImageError is replaced by "
                    "ModuleNotFoundError('No module named pi_heif'). pi-heif is "
                    "absent from requirements.txt. Any future container image "
                    "must pin this explicitly rather than letting inference "
                    "mutate its own environment."),
            },
            "model": {
                "path": os.path.relpath(model_path, ROOT).replace("\\", "/"),
                "md5": _md5(model_path) if os.path.isfile(model_path) else None,
                "bytes": os.path.getsize(model_path) if os.path.isfile(model_path) else None,
            },
            "thresholds": {
                "ROAD_DAMAGE_CONF_THRESHOLD": detector.CONFIDENCE_THRESHOLD,
                "ROAD_DAMAGE_ACCEPT_THRESHOLD": detector.ACCEPT_THRESHOLD,
                "ROAD_DAMAGE_FALLBACK_THRESHOLD": detector.FALLBACK_EVIDENCE_THRESHOLD,
                "RAW_CONF_FLOOR": detector.RAW_CONF_FLOOR,
            },
            "severity_map": detector.DAMAGE_SEVERITY,
            "runtime": {
                "python": ".".join(str(v) for v in sys.version_info[:3]),
                "ultralytics": ultralytics.__version__,
                "torch": torch.__version__,
                "cuda_available": bool(torch.cuda.is_available()),
                "device": "cuda" if torch.cuda.is_available() else "cpu",
            },
        },
        "cases": [],
        "negative_cases": [],
    }

    missing = [f for f, *_ in MANIFEST if not os.path.isfile(os.path.join(UPLOADS, f))]
    if missing:
        raise SystemExit("ABORT: manifest images missing from uploads/: %s" % missing)

    print("model      : %s" % detector.MODEL_PATH)
    print("thresholds : %s" % fixture["_meta"]["thresholds"])
    print("device     : %s" % fixture["_meta"]["runtime"]["device"])
    print("cases      : %d   negative_cases: %d" % (len(MANIFEST), len(NEGATIVE_CASES)))
    print("-" * 78)

    for name, det_id, db_class, db_conf, note in MANIFEST:
        path = os.path.join(UPLOADS, name)
        print("[%2d/%d] %s" % (len(fixture["cases"]) + 1, len(MANIFEST), name))

        entry = _image_meta(path)
        entry["selection_note"] = note
        try:
            verdict = detector.analyze_road(path)
            entry["analyze_road"] = verdict
            entry["run_detection"] = detector.run_detection(path)
            # severity_for is only meaningful when a class was named; recorded
            # as null otherwise rather than implying the model said "Warning"
            # about a normal road.
            damage_type = verdict.get("damage_type")
            entry["severity_for_damage_type"] = (
                detector.severity_for(damage_type) if damage_type else None)
            print("      -> %-22s %-22s conf=%s acc=%s bbox=%s"
                  % (verdict["road_condition"], str(damage_type),
                     verdict["confidence"], verdict["accepted"],
                     verdict["bbox"]))
        except Exception as exc:
            entry["analyze_road"] = None
            entry["run_detection"] = None
            entry["severity_for_damage_type"] = None
            entry["error"] = type(exc).__name__
            entry["error_message"] = str(exc)
            print("      -> ERROR %s: %s" % (type(exc).__name__, exc))

        entry["db_reference"] = (
            None if det_id is None else
            {"detection_id": det_id, "damage_class": db_class,
             "confidence": db_conf, "source": "smartsurround.db detections",
             "note": "provenance only, never asserted"})
        fixture["cases"].append(entry)

    print("-" * 78)
    for name in NEGATIVE_CASES:
        path = os.path.join(UPLOADS, name)
        entry = _image_meta(path)
        entry["selection_note"] = "corrupt upload; cannot be decoded"
        # What the PATCHED PIL.Image.open (i.e. what anything running under
        # ultralytics sees) does with an undecodable file.
        entry["decode_under_ultralytics_patch"] = _decode(
            path, __import__("PIL.Image", fromlist=["Image"]).open)
        try:
            verdict = detector.analyze_road(path)
            entry["analyze_road_result"] = verdict
            entry["raises"] = False
            print("[neg] %s -> NO EXCEPTION, analyze_road returned %s"
                  % (name, verdict))
        except Exception as exc:
            entry["analyze_road_result"] = None
            entry["raises"] = True
            entry["error"] = type(exc).__name__
            entry["error_message"] = str(exc)
            print("[neg] %s -> %s: %s" % (name, type(exc).__name__, exc))
        fixture["negative_cases"].append(entry)

    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fixture, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    print("-" * 78)
    print("wrote %s (%d bytes)" % (OUT_PATH, os.path.getsize(OUT_PATH)))


if __name__ == "__main__":
    build()

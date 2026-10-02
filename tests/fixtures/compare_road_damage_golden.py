"""
tests/fixtures/compare_road_damage_golden.py
---------------------------------------------
GOLDEN REGRESSION COMPARISON for `detector.py`.

Re-runs the Phase 1 manifest against the CURRENT detector and diffs the
result against the committed `road_damage_golden.json` baseline. Exits
non-zero on ANY drift.

This is deliberately NOT named `test_*.py`, so `python -m unittest discover
-s tests` does NOT pick it up: the unit suite stays hermetic and never loads
the 89 MB model (same convention as tests/test_hazard_contract.py). Run it
explicitly after touching detector.py, the weights, or the thresholds:

    python tests/fixtures/compare_road_damage_golden.py

    0  = detector behaviour identical to the baseline
    1  = DRIFT (at least one field changed) or the run could not complete

What is compared: everything that represents detector behaviour --
analyze_road(), run_detection(), severity_for(), plus the threshold /
severity-map / model-hash metadata, since changing any of those WOULD change
detector behaviour. Nothing is normalized or tolerated.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["YOLO_AUTOINSTALL"] = "false"   # must precede `import ultralytics`

# Reuse the generator's manifest and metadata helpers so the two tools can
# never drift apart. Importing is safe: `build()` is guarded by __main__.
sys.path.insert(0, HERE)
import generate_road_damage_golden as gen  # noqa: E402

FIXTURE = os.path.join(HERE, "road_damage_golden.json")

# The fields that ARE detector behaviour. Compared verbatim.
CASE_FIELDS = ("analyze_road", "run_detection", "severity_for_damage_type")
NEG_FIELDS = ("analyze_road_result", "raises")
META_FIELDS = ("thresholds", "severity_map")


def _walk(prefix, expected, actual, diffs):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in expected:
                diffs.append("%s.%s: ADDED %r" % (prefix, k, actual[k]))
            elif k not in actual:
                diffs.append("%s.%s: REMOVED %r" % (prefix, k, expected[k]))
            else:
                _walk("%s.%s" % (prefix, k), expected[k], actual[k], diffs)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append("%s: length %d -> %d" % (prefix, len(expected), len(actual)))
        for i in range(min(len(expected), len(actual))):
            _walk("%s[%d]" % (prefix, i), expected[i], actual[i], diffs)
    elif expected != actual:
        diffs.append("%s: %r -> %r" % (prefix, expected, actual))


def main():
    if not os.path.isfile(FIXTURE):
        print("ABORT: baseline missing: %s" % FIXTURE)
        return 1
    with open(FIXTURE, encoding="utf-8") as fh:
        baseline = json.load(fh)

    import detector
    import ultralytics

    print("model      : %s" % detector.MODEL_PATH)
    print("thresholds : %s" % {k: getattr(detector, v) for k, v in [
        ("conf", "CONFIDENCE_THRESHOLD"), ("accept", "ACCEPT_THRESHOLD"),
        ("fallback", "FALLBACK_EVIDENCE_THRESHOLD")]})
    print("baseline   : %d cases, %d negative cases"
          % (len(baseline["cases"]), len(baseline["negative_cases"])))
    print("-" * 78)

    diffs = []

    # --- metadata that would change behaviour if drifted -------------------
    live_meta = {
        "thresholds": {
            "ROAD_DAMAGE_CONF_THRESHOLD": detector.CONFIDENCE_THRESHOLD,
            "ROAD_DAMAGE_ACCEPT_THRESHOLD": detector.ACCEPT_THRESHOLD,
            "ROAD_DAMAGE_FALLBACK_THRESHOLD": detector.FALLBACK_EVIDENCE_THRESHOLD,
            "RAW_CONF_FLOOR": detector.RAW_CONF_FLOOR,
        },
        "severity_map": detector.DAMAGE_SEVERITY,
    }
    for f in META_FIELDS:
        _walk("_meta.%s" % f, baseline["_meta"][f], live_meta[f], diffs)

    base_model = baseline["_meta"].get("model") or {}
    if base_model.get("md5"):
        _walk("_meta.model.md5", base_model["md5"], gen._md5(detector.MODEL_PATH), diffs)
    base_rt = baseline["_meta"].get("runtime") or {}
    if base_rt.get("ultralytics") and base_rt["ultralytics"] != ultralytics.__version__:
        diffs.append("_meta.runtime.ultralytics: %r -> %r"
                     % (base_rt["ultralytics"], ultralytics.__version__))

    # --- per-case detector behaviour ---------------------------------------
    by_name = {c["image"].split("/")[-1]: c for c in baseline["cases"]}
    for i, (name, _det_id, _cls, _conf, _note) in enumerate(gen.MANIFEST, 1):
        path = os.path.join(gen.UPLOADS, name)
        expected = by_name.get(name)
        if expected is None:
            diffs.append("cases.%s: MISSING from baseline" % name)
            continue
        try:
            verdict = detector.analyze_road(path)
            actual = {
                "analyze_road": verdict,
                "run_detection": detector.run_detection(path),
                "severity_for_damage_type": (
                    detector.severity_for(verdict["damage_type"])
                    if verdict.get("damage_type") else None),
            }
        except Exception as exc:
            actual = {"analyze_road": None, "run_detection": None,
                      "severity_for_damage_type": None,
                      "_raised": type(exc).__name__}
        for f in CASE_FIELDS:
            _walk("cases.%s.%s" % (name, f), expected.get(f), actual.get(f), diffs)
        print("[%2d/%d] %-44s ok" % (i, len(gen.MANIFEST), name))

    # --- negative cases ----------------------------------------------------
    neg_by_name = {c["image"].split("/")[-1]: c for c in baseline["negative_cases"]}
    for name in gen.NEGATIVE_CASES:
        path = os.path.join(gen.UPLOADS, name)
        expected = neg_by_name.get(name)
        if expected is None:
            diffs.append("negative_cases.%s: MISSING from baseline" % name)
            continue
        try:
            verdict = detector.analyze_road(path)
            actual = {"analyze_road_result": verdict, "raises": False}
        except Exception as exc:
            actual = {"analyze_road_result": None, "raises": True,
                      "_raised": type(exc).__name__}
        for f in NEG_FIELDS:
            _walk("negative_cases.%s.%s" % (name, f), expected.get(f), actual.get(f), diffs)
        print("[neg]  %-44s ok" % name)

    print("-" * 78)
    if diffs:
        print("GOLDEN COMPARISON: DRIFT -- %d difference(s)" % len(diffs))
        for d in diffs:
            print("  - %s" % d)
        return 1
    print("GOLDEN COMPARISON: PASS -- detector behaviour identical to baseline "
          "(%d cases + %d negative cases, incl. thresholds, severity map, "
          "model hash)" % (len(gen.MANIFEST), len(gen.NEGATIVE_CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

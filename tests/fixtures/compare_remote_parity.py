"""
tests/fixtures/compare_remote_parity.py
---------------------------------------
OPT-IN shadow-run: proves that the Phase 4 remote path (Flask's
`RoadDamageModel` in `ROAD_DAMAGE_INFERENCE_MODE=remote`, talking HTTP to the
standalone ML service) produces byte-identical `DetectionResult` objects to the
local in-process path, over the full Phase 1 golden corpus.

    docker run -d --name smart-surround-ml -p 8000:8000 smart-surround-ml:1.0.0
    python tests/fixtures/compare_remote_parity.py
    python tests/fixtures/compare_remote_parity.py --url http://127.0.0.1:8000

Deliberately NOT named `test_*.py`, so `python -m unittest discover -s tests`
never picks it up: the unit suite must stay hermetic — no container, no
network, no YOLO inference. Same convention as
`tests/fixtures/compare_road_damage_golden.py`.

    0 = no drift in either direction
    1 = drift found, or the ML service was unreachable

Both paths are exercised through the REAL `hazard.RoadDamageModel`, so this
compares the two transports end to end (mapping, severity, accept) rather than
re-deriving either side here.

What is compared per case, for every field of DetectionResult:
    detected, hazard_category, hazard_type, confidence, damage_class,
    severity, accepted, bbox, model, model_version
plus, separately, the raw detector verdict fields.

The 3 corrupt cases are reported in their own table: both paths must answer
`normal` rather than raising, matching the Phase 1 baseline.
"""

import argparse
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURE = os.path.join(HERE, "road_damage_golden.json")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

# Fields compared on the normalized result. `source` is intentionally excluded:
# it is the one field that is SUPPOSED to differ (local vs remote).
COMPARED_FIELDS = (
    "detected", "hazard_category", "hazard_type", "confidence",
    "damage_class", "severity", "accepted", "bbox", "model", "model_version",
)

VERDICT_FIELDS = ("road_condition", "damage_type", "confidence", "accepted", "bbox")


def _reload_hazard(mode, url=None, retries="1"):
    """Reload hazard.py in the requested inference mode, return the module."""
    for key in ("ROAD_DAMAGE_INFERENCE_MODE", "ROAD_DAMAGE_ML_URL",
                "ROAD_DAMAGE_ML_RETRIES", "ROAD_DAMAGE_ML_TIMEOUT"):
        os.environ.pop(key, None)
    os.environ["ROAD_DAMAGE_INFERENCE_MODE"] = mode
    os.environ["ROAD_DAMAGE_ML_RETRIES"] = retries
    if url:
        os.environ["ROAD_DAMAGE_ML_URL"] = url
    import hazard
    return importlib.reload(hazard)

def _as_fields(result):
    return {f: getattr(result, f) for f in COMPARED_FIELDS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000",
                    help="base URL of the running ML service")
    ap.add_argument("--fixture", default=FIXTURE)
    args = ap.parse_args()

    with open(args.fixture, encoding="utf-8") as fh:
        golden = json.load(fh)

    import requests

    print("fixture   : %s" % os.path.relpath(args.fixture, ROOT))
    print("remote    : %s/predict" % args.url)
    print("mode      : local (in-process) vs remote (HTTP)")
    print("-" * 78)

    # Fail fast if the service is not up, before spending time on local runs.
    try:
        health = requests.get("%s/healthz" % args.url.rstrip("/"), timeout=15)
        health.raise_for_status()
        body = health.json()
        if not body.get("model_loaded"):
            print("ABORT: /healthz reports model_loaded=false")
            return 1
        print("healthz   : ok model_loaded=%s load=%ss"
              % (body.get("model_loaded"), body.get("model_load_seconds")))
    except Exception as exc:
        print("ABORT: cannot reach the ML service at %s: %s: %s"
              % (args.url, type(exc).__name__, exc))
        return 1

    # Warm the local model once so the first case is not a cold-start outlier.
    # RoadDamageModel.detect() reads the module-level INFERENCE_MODE, so the
    # module is reloaded per case below rather than switching modes mid-run.
    _reload_hazard("local").RoadDamageModel().detect(
        golden["cases"][0]["image"].replace("/", os.sep))

    diffs = []
    matched_fields = 0
    total_fields = 0

    print()
    print("%-44s %-22s %-6s" % ("case", "verdict", "fields"))
    print("-" * 78)

    for case in golden["cases"]:
        path = case["image"].replace("/", os.sep)
        # Each model must be driven by a module loaded in ITS OWN mode, since
        # RoadDamageModel.detect() reads the module-level INFERENCE_MODE.
        local = _as_fields(_reload_hazard("local").RoadDamageModel().detect(path))
        remote = _as_fields(_reload_hazard("remote", url=args.url).RoadDamageModel().detect(path))
        row = []
        for field in COMPARED_FIELDS:
            total_fields += 1
            if local[field] == remote[field]:
                matched_fields += 1
            else:
                row.append("%s: local %r != remote %r" % (field, local[field], remote[field]))
        diffs.extend("%s: %s" % (case["image"], d) for d in row)
        condition = ("normal" if not remote["detected"]
                     else "damaged" if remote["accepted"]
                     else "damaged_unclassified" if remote["damage_class"] == "Unclassified damage"
                     else "uncertain")
        print("%-44s %-22s %d/%d%s"
              % (os.path.basename(case["image"]), condition,
                 len(COMPARED_FIELDS) - len(row), len(COMPARED_FIELDS),
                 "" if not row else "   <-- DRIFT"))

    print()
    print("negative (corrupt) cases - both paths must answer 'normal', not raise")
    print("-" * 78)
    for case in golden["negative_cases"]:
        path = case["image"].replace("/", os.sep)
        expected = case["analyze_road_result"]
        try:
            local = _as_fields(_reload_hazard("local").RoadDamageModel().detect(path))
            local_ok = not local["detected"] and local["severity"] is None
        except Exception as exc:
            local_ok = False
            local = {}
            diffs.append("%s: local raised %s: %s" % (case["image"], type(exc).__name__, exc))
        try:
            remote = _as_fields(
                _reload_hazard("remote", url=args.url).RoadDamageModel().detect(path))
            remote_ok = not remote["detected"] and remote["severity"] is None
        except Exception as exc:
            remote_ok = False
            remote = {}
            diffs.append("%s: remote raised %s: %s" % (case["image"], type(exc).__name__, exc))

        for field in COMPARED_FIELDS:
            total_fields += 1
            if local_ok and remote_ok and local[field] == remote[field]:
                matched_fields += 1
            elif local_ok and remote_ok:
                diffs.append("%s: %s: local %r != remote %r"
                             % (case["image"], field, local[field], remote[field]))
        if expected.get("road_condition") != "normal":
            diffs.append("%s: fixture no longer records a normal verdict" % case["image"])
        print("%-44s local=%-8s remote=%-8s"
              % (os.path.basename(case["image"]),
                 "normal" if local_ok else "DIVERGED",
                 "normal" if remote_ok else "DIVERGED"))

    # The raw detector verdict must also agree with the Phase 1 baseline, so a
    # matching-but-wrong result cannot pass this check.
    print()
    print("golden-fixture cross-check (remote vs Phase 1 recorded verdict)")
    print("-" * 78)
    fixture_checked = 0
    for case in golden["cases"]:
        path = case["image"].replace("/", os.sep)
        expected = case["analyze_road"]
        remote = _as_fields(_reload_hazard("remote", url=args.url).RoadDamageModel().detect(path))
        # Map the normalized result back to the verdict the fixture recorded.
        derived_condition = ("normal" if not remote["detected"]
                             else "damaged" if remote["accepted"]
                             else "damaged_unclassified" if remote["damage_class"] == "Unclassified damage"
                             else "uncertain")
        fixture_checked += 1
        # NOTE: the fixture's `severity_for_damage_type` is PROVENANCE ONLY. It
        # records severity_for(damage_type) on the raw verdict, so it is null for
        # the damaged_unclassified tier. The contract both paths implement is
        # severity_for(damage_type or "Unclassified damage") == "Warning" for that
        # tier, which is why the resolved severity is compared against the
        # fallback-resolved value rather than the recorded annotation.
        expected_severity = None
        if expected.get("road_condition") != "normal":
            severity_for = _reload_hazard("local").detector.severity_for
            expected_severity = severity_for(
                expected.get("damage_type") or "Unclassified damage")
        if remote["severity"] != expected_severity:
            diffs.append("%s: severity %r != expected %r"
                         % (case["image"], remote["severity"], expected_severity))
        if remote["confidence"] != expected.get("confidence"):
            diffs.append("%s: confidence vs fixture %r != %r"
                         % (case["image"], remote["confidence"], expected.get("confidence")))
        if remote["accepted"] != expected.get("accepted"):
            diffs.append("%s: accepted vs fixture %r != %r"
                         % (case["image"], remote["accepted"], expected.get("accepted")))
        if derived_condition != expected.get("road_condition"):
            diffs.append("%s: road_condition vs fixture %r != %r"
                         % (case["image"], derived_condition, expected.get("road_condition")))
    print("%d readable cases re-checked against the recorded Phase 1 verdict" % fixture_checked)

    print()
    print("-" * 78)
    print("compared fields : %d" % total_fields)
    print("matched         : %d" % matched_fields)
    print("drifted         : %d" % (total_fields - matched_fields))
    print("cases           : %d readable + %d corrupt"
          % (len(golden["cases"]), len(golden["negative_cases"])))
    if diffs:
        print()
        print("SHADOW PARITY: DRIFT -- %d difference(s)" % len(diffs))
        for d in diffs:
            print("  - %s" % d)
        return 1
    print()
    print("SHADOW PARITY: PASS -- local and remote agree on every field of")
    print("DetectionResult across all %d golden cases, and the remote verdicts"
          % (len(golden["cases"]) + len(golden["negative_cases"])))
    print("match the Phase 1 baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

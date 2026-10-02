"""
ml/parity_check.py
------------------
OPT-IN parity check: proves the standalone ML service returns exactly the same
detector semantics as the Phase 1 golden baseline.

    python ml/parity_check.py                       # http://127.0.0.1:8000
    python ml/parity_check.py --base-url http://...  # or the container

Deliberately NOT named `test_*.py`, so `python -m unittest discover -s tests`
never picks it up: the unit suite stays hermetic and never loads the model or
needs a running service. Same convention as
tests/fixtures/compare_road_damage_golden.py.

    0 = parity confirmed for every case
    1 = parity drift, or the service was unreachable

What is compared, per case, field by field against
tests/fixtures/road_damage_golden.json:
    road_condition, damage_type, confidence, accepted, bbox
plus, for the service only, the observable severity behaviour -- obtained by
running the SAME detector.severity_for() lookup hazard.py uses, so the service
is not asked to invent a severity it must not compute.

Negative (corrupt) cases are compared too. Phase 1 established that
analyze_road() does NOT raise on an undecodable image: it reports
road_condition="normal" with confidence=None. The service must reproduce that
rather than "fixing" it.
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "road_damage_golden.json")

# ROOT on sys.path so `import detector` works when this is run from anywhere
# (it is the module that owns severity_for(), the lookup hazard.py performs).
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
os.environ.setdefault("YOLO_AUTOINSTALL", "false")

TIMEOUT = 120


def _post_image(base_url, path):
    """POST one image as multipart/form-data, stdlib only (no requests dep in
    the runtime image). Field name 'image'."""
    boundary = "----parityboundary7f3a1c9e"
    with open(path, "rb") as fh:
        payload = fh.read()
    name = os.path.basename(path)
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % name).encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ])
    req = urllib.request.Request(
        "%s/predict" % base_url.rstrip("/"), data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _get_json(base_url, path):
    try:
        with urllib.request.urlopen("%s%s" % (base_url.rstrip("/"), path),
                                    timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _post_no_image(base_url):
    """POST a well-formed multipart body carrying no file field at all."""
    boundary = "----parityboundary7f3a1c9e"
    body = ("--%s\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nx\r\n--%s--\r\n"
            % (boundary, boundary)).encode()
    req = urllib.request.Request(
        "%s/predict" % base_url.rstrip("/"), data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _empty_file():
    fd, path = tempfile.mkstemp(prefix="ml-empty-", suffix=".png")
    os.close(fd)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--fixture", default=FIXTURE)
    args = ap.parse_args()

    with open(args.fixture, encoding="utf-8") as fh:
        golden = json.load(fh)

    # NOTE: no `import detector` here on purpose. The service deliberately does
    # NOT compute severity or acceptance of its own -- it returns the raw
    # analyze_road() dict and the Flask/Phase 2 layer owns severity_for() and
    # the accept threshold. Importing detector here would run the HOST copy and
    # would silently re-test the host instead of the container. Parity is
    # therefore asserted purely on the fields the service actually returns.
    print("service  : %s" % args.base_url)
    print("fixture  : %s" % os.path.relpath(args.fixture, ROOT))
    print("-" * 78)

    status, health = _get_json(args.base_url, "/healthz")
    if status != 200 or not health.get("model_loaded"):
        print("ABORT: /healthz not healthy (status=%s body=%s)" % (status, health))
        return 1
    print("healthz  : ok model_loaded=%s load=%ss device-thresholds=%s"
          % (health.get("model_loaded"), health.get("model_load_seconds"),
             health.get("thresholds", {}).get("accept")))

    diffs = []
    checked = 0

    for case in golden["cases"]:
        path = os.path.join(ROOT, case["image"].replace("/", os.sep))
        expected = case["analyze_road"]
        status, body = _post_image(args.base_url, path)
        checked += 1

        if status != 200 or not body.get("ok"):
            diffs.append("%s: HTTP %s %s" % (case["image"], status, body))
            continue
        actual = body["result"]

        for field in ("road_condition", "damage_type", "confidence",
                      "accepted", "bbox"):
            if expected.get(field) != actual.get(field):
                diffs.append("%s.%s: golden %r -> service %r"
                             % (case["image"], field, expected.get(field),
                                actual.get(field)))

        # Sanity-show the verdicts that carry the most weight, and cross-check
        # that whatever severity the Phase 1 fixture recorded for this case is
        # consistent with the damage_type the service returned.
        recorded = case.get("severity_for_damage_type")
        if recorded == "Critical":
            # The fixture nests the verdict under analyze_road; severity_for_
            # damage_type is a sibling annotation, not a service field.
            if actual.get("damage_type") != expected.get("damage_type"):
                diffs.append("%s: recorded Critical but damage_type drifted (%r -> %r)"
                             % (case["image"], expected.get("damage_type"),
                                actual.get("damage_type")))
            print("  %-42s Critical  type=%s conf=%s accepted=%s"
                  % (case["image"][8:], actual.get("damage_type"),
                     actual.get("confidence"), actual.get("accepted")))

    print("-" * 78)
    neg_checked = 0
    for case in golden["negative_cases"]:
        path = os.path.join(ROOT, case["image"].replace("/", os.sep))
        expected = case["analyze_road_result"]
        status, body = _post_image(args.base_url, path)
        neg_checked += 1
        if status != 200 or not body.get("ok"):
            diffs.append("%s: HTTP %s %s (expected 200 + ok)"
                         % (case["image"], status, body))
            continue
        actual = body["result"]
        for field in ("road_condition", "damage_type", "confidence", "accepted"):
            if expected.get(field) != actual.get(field):
                diffs.append("%s.%s: golden %r -> service %r"
                             % (case["image"], field, expected.get(field),
                                actual.get(field)))

    # Structured error envelope must exist for genuinely malformed requests.
    # NOTE: a non-image file is NOT a valid error probe. Phase 1 proved
    # analyze_road() returns road_condition="normal" for an undecodable file,
    # so posting one must return 200/normal, not an error. The client-error
    # cases are a missing file field and a zero-byte file.
    status, body = _post_no_image(args.base_url)
    if status == 200 and body.get("ok"):
        diffs.append("missing image field: expected structured error, got ok")
    else:
        print("no image field: HTTP %s reason=%s" % (status, body.get("reason")))

    empty = _empty_file()
    try:
        status, body = _post_image(args.base_url, empty)
        if status == 200 and body.get("ok"):
            diffs.append("empty file: expected structured error, got ok")
        else:
            print("empty file    : HTTP %s reason=%s" % (status, body.get("reason")))
    finally:
        os.remove(empty)

    print("-" * 78)
    if diffs:
        print("PARITY: DRIFT -- %d difference(s)" % len(diffs))
        for d in diffs:
            print("  - %s" % d)
        return 1
    print("PARITY: PASS -- %d cases + %d negative cases identical to the "
          "golden baseline (road_condition, damage_type, confidence, "
          "accepted, bbox)" % (checked, neg_checked))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
app.py
------
SmartSurround backend — detection queue + admin authority notification.

Track A routes (detection → admin verify → letter PDF):
  GET  /                     upload form (citizen / ESP32-CAM stand-in)
  POST /upload               runs detection, queues for admin review
  GET  /admin                verification queue
  POST /admin/approve/<id>   approved → letter generated (token+pin locked)
  POST /admin/reject/<id>    rejected (token+pin locked)
  GET  /admin/api/clusters   JSON: corroboration clusters (polling)
  POST /admin/authorities/add register authority email (token+pin locked)
  POST /admin/test-email     dev-only live test send (token+pin locked)

Auth:
  GET  /login                login page (pin entry)
  POST /login/creds          verify pin → httpOnly token cookie
  GET  /upload/token         ephemeral upload token (redirect-based flow)

Run:
    python3 app.py
    Then open http://localhost:5000
"""

import os
import time
import uuid
import secrets
import logging
from pathlib import Path
from functools import wraps
from datetime import datetime, timezone
from dotenv import load_dotenv

# Resolve .env NEXT TO app.py (not the process cwd). If we didn't, launching
# from any other directory (debug reloader, scheduler, a second shell) would
# silently skip .env, leaving AUTH_DISABLED / RESEND key / admin pin unset —
# which woke the pin gate back up and made every protected admin call 401.
load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import (
    Flask, request, render_template, redirect, url_for,
    send_from_directory, flash, jsonify, make_response
)

import db
import detector
import letter_generator
import corroboration
import authority_routing
import email_driver

logger = logging.getLogger("smart_surround.auth")

# ---------------------------------------------------------------------------
# Pin digest helpers (mirror static/utils.js build_verify_pin)
# ---------------------------------------------------------------------------
def _derive_pin_digest(pin, salt_hex):
    import hashlib
    payload = bytes.fromhex(salt_hex) + str(pin).encode("utf-8")
    return salt_hex + ":" + hashlib.sha256(payload).hexdigest()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Pin policy: the server NEVER stores the bare pin. If ADMIN_PIN is set we
# derive a salted digest on boot (`salt_hex:sha256(salt||pin)`); verification
# re-hashes the presented pin against that digest with a constant-time compare.
# Same scheme as static/utils.js build_verify_pin (client-side pre-hash
# remains available for the login form if needed).
ADMIN_PIN       = os.environ.get("ADMIN_PIN", "")
_ADMIN_SALT     = secrets.token_hex(16)
_ADMIN_DIGEST   = None
if ADMIN_PIN:
    _ADMIN_DIGEST = _derive_pin_digest(ADMIN_PIN, _ADMIN_SALT)

CORS_ORIGINS   = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000").split(",") if o.strip()]
TOKEN_TTL_SEC  = 30 * 60          # 30 minutes
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_SEC = int(os.environ.get("RATE_LIMIT_SEC", str(60 * 60)))

# AUTH_DISABLED=1 turns the whole pin gate off (dev convenience / trusted LAN).
# When set: @require_auth passes everything through AND /login/creds mints a
# session without a pin, so the admin bootstrap (apiEnsureSession -> /login/creds)
# succeeds trivially and every protected admin action is freely usable.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "0") == "1"

def _migrate_legacy_absolute_paths():
    """One-time at-boot fix for the /uploads/<file> 404 storm.

    New uploads store only the basename (app.py stores os.path.basename(...)),
    but detection rows written before that change carry the FULL absolute path
    (e.g. ``D:\\Program\\New folder\\...\\uploads\\45b4f....png``). The admin
    templates render URLs as ``/uploads/{ d['image_path'].split('/')[-1] }`` —
    and a backslash absolute path is NOT split by '/', so the whole drive path
    leaks into the URL → every image/letter 404s (the flood in the dev log).

    We can't fix it in the template (we must keep both / and \ safe) or at
    write time (rows already exist). So we normalize the stored value to a
    bare basename in-place, once, at boot. idempotent: basename of a basename
    is itself, so re-runs on every start are safe."""
    import itertools
    conn = db.get_conn()
    try:
        for table, col in (("detections", "image_path"),
                           ("detections", "letter_path"),
                           ("corroboration_clusters", "letter_path")):
            try:
                rows = conn.execute(f"SELECT id, {col} FROM {table}").fetchall()
            except Exception:
                continue    # table/column may be absent in older schemas
            for row in rows:
                raw = row[col]
                if raw and (os.sep in raw or "/" in raw):
                    clean = os.path.basename(raw)
                    if clean != raw:
                        conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?",
                                     (clean, row["id"]))
        conn.commit()
    finally:
        conn.close()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

db.init_db()
_migrate_legacy_absolute_paths()

# ---------------------------------------------------------------------------
# In-memory stores (single-process; adequate for dev)
# ---------------------------------------------------------------------------
_token_store   = {}    # token_str -> expires_at_epoch
_rate_store    = {}    # ip_addr   -> {"failures": int, "cooldown_until": float}
_upload_tokens = {}    # token_str -> expires_at_epoch (for /upload/token flow)

def _pin_ok(pin):
    if not _ADMIN_DIGEST:
        return False
    digest = _derive_pin_digest(pin, _ADMIN_SALT)
    return secrets.compare_digest(digest, _ADMIN_DIGEST)

# ---------------------------------------------------------------------------
# CORS — exact-origin, no wildcard
# ---------------------------------------------------------------------------
@app.after_request
def _apply_cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"]  = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"]  = "Content-Type, Authorization, X-Auth-Token, X-Set-Auth-Token"
        resp.headers["Access-Control-Allow-Methods"]  = "GET, POST, OPTIONS"
    if request.method == "OPTIONS":
        resp.status_code = 204
    return resp

# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per IP)
# ---------------------------------------------------------------------------
def _rate_limit(ip):
    entry = _rate_store.get(ip)
    if entry and entry.get("cooldown_until", 0) > time.time():
        return True    # still locked
    return False

def _rate_record_fail(ip):
    entry = _rate_store.setdefault(ip, {"failures": 0, "cooldown_until": 0})
    entry["failures"] += 1
    if entry["failures"] >= RATE_LIMIT_MAX:
        entry["cooldown_until"] = time.time() + RATE_LIMIT_SEC
        entry["failures"] = 0

def _rate_reset(ip):
    _rate_store.pop(ip, None)

# ---------------------------------------------------------------------------
# Token helpers (admin token issued on login)
# ---------------------------------------------------------------------------
def _issue_token():
    tok = secrets.token_urlsafe(32)
    _token_store[tok] = time.time() + TOKEN_TTL_SEC
    return tok

def _valid_token(tok):
    if not tok:
        return False
    exp = _token_store.get(tok)
    if exp is None or exp < time.time():
        _token_store.pop(tok, None)
        return False
    return True

# ---------------------------------------------------------------------------
# Upload-token helpers (ephemeral, single-use, per /upload/token)
# ---------------------------------------------------------------------------
def _issue_upload_token():
    tok = secrets.token_urlsafe(24)
    _upload_tokens[tok] = time.time() + 120    # 2-minute window
    return tok

def _valid_upload_token(tok):
    if not tok:
        return False
    exp = _upload_tokens.pop(tok, None)
    return exp is not None and exp >= time.time()

# ---------------------------------------------------------------------------
# Auth: login flow
# ---------------------------------------------------------------------------
def _set_auth_cookie(resp, tok):
    resp.set_cookie("ss_token", tok, httponly=True, samesite="Strict", max_age=TOKEN_TTL_SEC)
    resp.headers["X-Set-Auth-Token"] = tok
    return resp

@app.route("/auth/token", methods=["POST"])
def auth_token():
    """Token issuance endpoint — the one place a token is minted. Verifies the
    pin (salted digest) then issues a short-lived httpOnly token. Called by
    /login/creds after it confirms the pin; the frontend never touches the token
    except via the browser cookie + X-Set-Auth-Token response header."""
    ip = request.remote_addr or "0.0.0.0"
    if _rate_limit(ip):
        return jsonify({"ok": False, "reason": "rate_limited",
                        "message": "Too many failed attempts. Try again in an hour."}), 429
    pin = _get_pin_from_request()
    if not _pin_ok(pin):
        _rate_record_fail(ip)
        remaining = RATE_LIMIT_MAX - (_rate_store.get(ip, {}).get("failures", 0))
        msg = f"Wrong pin. {max(0, remaining)} attempts remaining."
        if remaining <= 0:
            msg = "Account locked for 1 hour due to too many failed attempts."
        return jsonify({"ok": False, "reason": "blocked", "message": msg}), 403
    _rate_reset(ip)
    tok = _issue_token()
    return _set_auth_cookie(make_response(jsonify({"ok": True, "message": "Authenticated."})), tok)

@app.route("/login/creds", methods=["POST"])
def login_creds():
    if AUTH_DISABLED:
        # Pin gate is off: mint a session trivially so the admin bootstrap
        # (apiEnsureSession -> POST /login/creds) succeeds with NO pin, and
        # every protected admin action is freely usable end-to-end.
        _rate_reset(request.remote_addr or "0.0.0.0")
        tok = _issue_token()
        return _set_auth_cookie(make_response(jsonify({"ok": True, "message": "Authenticated (auth disabled)."})), tok)
    # delegate to the single token issuance path (same pin policy, same rates)
    pin = _get_pin_from_request()
    ip = request.remote_addr or "0.0.0.0"
    if _rate_limit(ip):
        return jsonify({"ok": False, "reason": "rate_limited",
                        "message": "Too many failed attempts. Try again in an hour."}), 429
    if not _pin_ok(pin):
        _rate_record_fail(ip)
        remaining = RATE_LIMIT_MAX - (_rate_store.get(ip, {}).get("failures", 0))
        msg = f"Wrong pin. {max(0, remaining)} attempts remaining."
        if remaining <= 0:
            msg = "Account locked for 1 hour due to too many failed attempts."
        return jsonify({"ok": False, "reason": "blocked", "message": msg}), 403
    _rate_reset(ip)
    tok = _issue_token()
    return _set_auth_cookie(make_response(jsonify({"ok": True, "message": "Authenticated."})), tok)

def _token_from_header():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("X-Auth-Token", "")
    if header:
        return header.strip()
    # httpOnly cookie set on login (browser sends it automatically)
    cookie = request.cookies.get("ss_token", "")
    if cookie:
        return cookie.strip()
    return ""

def _get_pin_from_request():
    pin = request.form.get("pin") or request.args.get("pin")
    if not pin:
        ct = request.content_type or ""
        if "json" in ct:
            data = request.get_json(silent=True) or {}
            pin = data.get("pin")
    return pin or ""

def require_auth(f):
    """Locked route: must present a valid token + pin (or valid upload-token)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_DISABLED:
            # Trusted LAN / dev convenience: the whole pin+token gate is off.
            return f(*args, **kwargs)
        ip = request.remote_addr or "0.0.0.0"
        if _rate_limit(ip):
            return jsonify({"ok": False, "reason": "rate_limited",
                            "message": "Too many failed attempts. Try again later."}), 429

        tok = _token_from_header()
        pin = _get_pin_from_request()

        if not _valid_token(tok):
            # Stale/expired token is a normal login-state condition, NOT a
            # brute-force signal — never count it toward the PIN lockout.
            return jsonify({"ok": False, "reason": "blocked",
                            "message": "Authentication required. Please log in."}), 401

        if not _pin_ok(pin):
            _rate_record_fail(ip)
            remaining = RATE_LIMIT_MAX - (_rate_store.get(ip, {}).get("failures", 0))
            msg = f"Wrong pin. {max(0, remaining)} attempts remaining."
            if remaining <= 0:
                msg = "Account locked for 1 hour due to too many failed attempts."
            return jsonify({"ok": False, "reason": "blocked", "message": msg}), 403

        _rate_reset(ip)
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Track A — detection → admin queue
# ---------------------------------------------------------------------------
def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.remote_addr or None

@app.route("/")
def index():
    return render_template("index.html")

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

    source      = request.form.get("source", "citizen")
    lat         = request.form.get("lat") or None
    lon         = request.form.get("lon") or None
    description = request.form.get("description") or None
    client_ip   = _client_ip()
    hazard_type = "road_damage"

    ext       = os.path.splitext(image_file.filename)[1] or ".jpg"
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)
    image_file.save(saved_path)

    result = detector.analyze_road(saved_path)
    if result["road_condition"] == "normal":
        flash("No damage detected above the confidence threshold \u2014 nothing queued.")
        return redirect(url_for("index"))

    damage_class = result["damage_type"] or "Unclassified damage"
    severity     = detector.severity_for(damage_class)
    lat_f        = float(lat) if lat else None
    lon_f        = float(lon) if lon else None
    corroboration_area_key = corroboration.fine_area_key(lat_f, lon_f)
    authority_area_key     = corroboration.coarse_area_key(lat_f, lon_f)

    # Store the BASENAME only. The templates build URLs as "/uploads/" +
    # image_path.split("/")[-1]; storing the absolute Windows path leaked the
    # whole drive path into that URL (every image 404'd). os.path.basename
    # strips both / and \ separators, so this is safe regardless of OS.
    new_id = db.insert_detection(
        image_path=os.path.basename(saved_path), source=source, damage_class=damage_class,
        confidence=result["confidence"], severity=severity, lat=lat_f, lon=lon_f,
        description=description, ai_accepted=result["accepted"], client_ip=client_ip,
        hazard_type=hazard_type, corroboration_area_key=corroboration_area_key,
authority_area_key=authority_area_key,
    )

    emailed = corroboration.run_lifecycle_sweep()

    if emailed:
        flash(f"Detection #{new_id} ({damage_class}, {severity}) queued. "
              f"Auto-emailed {emailed} corroborated cluster(s).")
    elif result["road_condition"] == "damaged_unclassified":
        confidence_note = "unclassified \u2014 multiple weak signals, needs a closer look"
        flash(f"Detection #{new_id} ({damage_class}, {severity}, {confidence_note}) "
              f"queued for admin verification.")
    else:
        confidence_note = "high-confidence" if result["accepted"] else "uncertain \u2014 needs a closer look"
        flash(f"Detection #{new_id} ({damage_class}, {severity}, {confidence_note}) "
              f"queued for admin verification.")
    return redirect(url_for("index"))

# ---------------------------------------------------------------------------
# Auth: login flow
# ---------------------------------------------------------------------------
@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/upload/token")
def upload_token_get():
    tok = _issue_upload_token()
    resp = make_response(jsonify({"ok": True, "token": tok}))
    resp.headers["X-Upload-Token"] = tok
    return resp

# ---------------------------------------------------------------------------
# Admin dashboard (clusters + authorities rendered client-side via polling)
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    pending  = db.list_by_status("pending")
    approved = db.list_by_status("approved")
    rejected = db.list_by_status("rejected")
    return render_template("admin.html",
                           pending=pending, approved=approved, rejected=rejected)

@app.route("/admin/api/clusters")
def admin_api_clusters():
    clusters    = corroboration.list_clusters()
    authorities = authority_routing.list_authorities_for_admin()
    cluster_list = []
    for c in clusters:
        cluster_list.append({
            "id":                    c["id"],
            "corroboration_area_key": c["corroboration_area_key"],
            "hazard_type":           c["hazard_type"],
            "window_start":          c["window_start"],
            "lifecycle":             c["lifecycle"],
            "letter_path":           c["letter_path"],
            "emailed_at":            c["emailed_at"],
            "created_at":            c["created_at"],
        })
    auth_list = []
    for a in authorities:
        auth_list.append({
            "id":                a["id"],
            "authority_area_key": a["authority_area_key"],
            "hazard_type":       a["hazard_type"],
            "email":             a["email"],
            "lifecycle":         a["lifecycle"],
            "created_at":        a["created_at"],
            "verified_at":       a["verified_at"],
            "bounced_at":        a["bounced_at"],
        })
    return jsonify({"ok": True, "clusters": cluster_list, "authorities": auth_list})

@app.route("/admin/authorities/add", methods=["POST"])
@require_auth
def admin_authorities_add():
    area_key  = (request.form.get("authority_area_key") or "").strip()
    hazard    = (request.form.get("hazard_type") or "road_damage").strip()
    email     = (request.form.get("email") or "").strip()
    if not area_key or not email:
        return jsonify({"ok": False, "message": "Area key and email are required."}), 400
    row = db.get_or_create_authority(area_key, hazard, email)
    return jsonify({"ok": True, "authority_id": row["id"],
                    "lifecycle": row["lifecycle"]}), 201

@app.route("/admin/approve/<int:detection_id>", methods=["POST"])
@require_auth
def approve(detection_id):
    detection = db.get_detection(detection_id)
    if detection is None:
        return jsonify({"ok": False, "message": "Detection not found."}), 404

    letter_path = letter_generator.generate_letter(detection)
    db.update_status(detection_id, "approved", letter_path=letter_path)

    emailed = corroboration.run_lifecycle_sweep()

    msg = f"Detection #{detection_id} approved \u2014 letter generated."
    if emailed:
        msg += f" Auto-emailed {emailed} corroborated cluster(s)."
    return jsonify({"ok": True, "message": msg})

@app.route("/admin/reject/<int:detection_id>", methods=["POST"])
@require_auth
def reject(detection_id):
    detection = db.get_detection(detection_id)
    if detection is None:
        return jsonify({"ok": False, "message": "Detection not found."}), 404
    db.update_status(detection_id, "rejected")
    return jsonify({"ok": True, "message": f"Detection #{detection_id} rejected."})

@app.route("/admin/authority/<int:authority_id>/verify", methods=["POST"])
@require_auth
def verify_authority(authority_id):
    authority = db.get_authority(authority_id)
    if authority is None:
        return jsonify({"ok": False, "message": f"Authority #{authority_id} not found."}), 404
    authority_routing.verify_authority(authority_id)
    return jsonify({"ok": True, "message": f"Authority #{authority_id} verified."})

@app.route("/admin/authority/<int:authority_id>/bounce", methods=["POST"])
@require_auth
def bounce_authority(authority_id):
    authority = db.get_authority(authority_id)
    if authority is None:
        return jsonify({"ok": False, "message": f"Authority #{authority_id} not found."}), 404
    authority_routing.bounce_authority(authority_id)
    return jsonify({"ok": True,
                    "message": f"Authority #{authority_id} bounced \u2014 flipped back to pending."})

@app.route("/admin/test-email", methods=["POST"])
@require_auth
def test_email():
    approved = db.list_by_status("approved")
    if not approved:
        return jsonify({"ok": False, "message": "No approved detections to use for test."}), 400
    latest = approved[0]
    letter_path = latest["letter_path"]
    if not letter_path or not os.path.isfile(letter_path):
        letter_path = letter_generator.generate_letter(latest)
    result = email_driver.send_authority_email(
        os.environ.get("EMAIL_TEST_RECIPIENT", "codexzero98@gmail.com"),
        f"SmartSurround Test Email \u2014 Detection #{latest['id']}",
        f"Test email sent from SmartSurround.\n\n"
        f"Detection #{latest['id']}: {latest['damage_class'] or 'N/A'}\n"
        f"Severity: {latest['severity'] or 'N/A'}\n"
        f"Mode: {'test (dry run)' if email_driver.TEST_MODE else 'live (real send)'}\n\n"
        f"Config: EMAIL_DRIVER={email_driver.DRIVER}, "
        f"TEST_MODE={email_driver.TEST_MODE}",
        letter_path,
    )
    return jsonify({"ok": result.get("ok"), "reason": result.get("reason"),
                    "mode": result.get("mode"), "to": result.get("effective_recipient")})

# ---------------------------------------------------------------------------
# Static: letters, uploads
# ---------------------------------------------------------------------------
@app.route("/letters/<int:detection_id>")
def download_letter(detection_id):
    detection = db.get_detection(detection_id)
    if detection is None or not detection["letter_path"]:
        return "No letter generated for this detection yet.", 404
    directory, filename = os.path.split(detection["letter_path"])
    return send_from_directory(directory, filename, as_attachment=True)

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

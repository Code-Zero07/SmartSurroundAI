# SmartSurround Backend — File Structure & Responsibilities

**Root:** `D:\Program\New folder\smartsurround_backend02\smartsurround_backend`
**Date of snapshot:** 2026-09-16 · **Status:** Track A regression-locked, Track B (corroboration + authority-email) additive

---

## 1. One-line map

```
smartsurround_backend/
├── app.py                Flask application (Track A + additive Track B wiring)
├── db.py                 SQLite data layer (Track A surface + Track B schema/helpers)
├── detector.py           Road-damage detection (YOLOv8) + severity mapping (Track A)
├── corroboration.py      Additive: corroboration clusters, derived counts, lifecycle sweep
├── authority_routing.py  Additive: authority ownership + route pending clusters to email/admin
├── email_driver.py       Additive: email driver (test-mode default, smtplib switch), letter send
├── letter_generator.py   Track A: generate authority letter PDF from a detection
├── requirements.txt      Python deps (incl. python-dotenv, ultralytics, Flask)
├── .env.example          Track B env surface (email + corroboration knobs, safe defaults)
├── README.md             Project readme (Track A + Track B addendum)
├── models/               road_damage_yolov8s.pt — YOLOv8s weights used by detector.py
├── templates/            index.html (citizen upload), admin.html (admin queue; Track B rows)
├── static/               app.js front-end asset served by Flask
├── uploads/              uploaded road photos (41 files, incl. .gitkeep)
├── letters/              generated authority letter PDFs (letter_detection_<id>.pdf)
├── smartsurround.db      SQLite database (detections + additive corroboration/authority data)
├── docs/                 specs/plans/worklog (see §4)
├── skills/               opencode skill packs (brainstorming, frontend-design, writing-plans)
├── venv/                 Python 3.13 virtualenv (project-local interpreter)
└── __pycache__/          compiled bytecode (created at import time)
```

---

## 2. Module responsibilities (verified on-disk, boolean-gated)

### 2.1 `app.py` — Flask application (orchestrator)
- Boots the Flask app (`boot_ok True`); serves citizen upload, admin queue, letter download.
- **Track A surfaces (byte-intact):** `/` (upload form), `/upload` (POST → detector → DB), `/admin` (pending list), `/admin/approve/<id>`, `/admin/reject/<id>`, `/letters/<id>`, `/uploads/<file>`.
- **Track B additive wiring:** the upload/admin path calls `corroboration.run_lifecycle_sweep()` and `authority_routing.route_pending_clusters()` (both previously-missing names were fixed this session; now callable + no-raise on empty).
- Real HTTP verified: `GET /` 200, `GET /admin` 200, `POST /upload` ≠ 500, upload row carries all four Track B additive columns.

### 2.2 `db.py` — SQLite data layer
- **Track A six-surface (regression-locked, all callable `True`):**
  `insert_detection(image_path, source, damage_class, confidence, severity, lat, lon, description, ai_accepted)`,
  `update_status(id, status, letter_path)`, `list_by_status(status)`, `get_detection(id)`, plus connection/init helpers.
- **Track B additive schema** (column/table migrations additive; nothing Track A removed):
  - `detections` additive columns: `client_ip`, `hazard_type`, `corroboration_area_key`, `authority_area_key`.
  - `corroboration_clusters` (fine area + hazard + window, lifecycle open→corroborated→…).
  - `authorities` (area + hazard + email, lifecycle pending/verified/bounced).
- **Track B helpers:** `list_clusters`, `get_or_create_cluster`, `update_cluster(...)`, `get_cluster_by_window`, `list_authorities`, `get_authority`, `get_verified_authority`, `mark_authority_verified`, `mark_authority_bounced`.

### 2.3 `detector.py` — road-damage detection (Track A, untouched)
- `analyze_road(image_path)` → dict with `road_condition`, `damage_type`, `confidence`, `accepted`.
- `severity_for(damage_class)` → severity label. + `(models/road_damage_yolov8s.pt)`.

### 2.4 `corroboration.py` — corroboration engine (Track B additive)
- Area cell derivation: `fine_area_key(lat, lon)`, `coarse_area_key(lat, lon)` (fine ~3 decimals, coarse ~2).
- `corroboration_count(...)` — **always derived** (COUNT of distinct reporters per fine-area + hazard + matured window), never a stored counter.
- Window helpers (`window_start_of`, `window_end_of`, `window_matured`), threshold constant (`CORROBORATION_THRESHOLD` >= 3, env-tunable).
- `run_lifecycle_sweep()` — matured window lifecycle flip surfaced here (callable, no-raise on empty).

### 2.5 `authority_routing.py` — authority ownership + routing (Track B additive)
- `route_cluster(...)` — route decision: **auto-email** (verified authority) vs **admin queue** (pending/bounced/unowned).
- `route_pending_clusters()` — additive surface (callable, no-raise on empty).
- Lifecycle flip pending → verified → bounced → pending (verified–bounced is a flip, never an added tier).

### 2.6 `email_driver.py` — email sender (Track B additive)
- Driver switch `EMAIL_DRIVER` (default smtplib), **`EMAIL_TEST_MODE` defaults true** → logs intended recipient (`EMAIL_TEST_RECIPIENT`), never sends, never needs credentials, never crashes.
- `send_authority_email(...)` — builds authority letter and hands it to the active driver; `smtplib_std True` (stdlib importable).

### 2.7 `letter_generator.py` — authority letter PDF (Track A, untouched)
- `generate_letter(detection)` → `letter_path` (PDF written into `letters/`; e.g. `letter_detection_3.pdf`).

---

## 3. Non-.py assets

| Path | Does what |
|---|---|
| `models/road_damage_yolov8s.pt` | YOLOv8s road-damage weights loaded by `detector.py` |
| `templates/index.html` | Citizen upload form (Track A) |
| `templates/admin.html` | Admin queue page (Track A + Track B additive rows/columns) |
| `static/app.js` | Front-end asset served by the Flask app |
| `uploads/…` | Uploaded road photos (stored by upload route; 41 at snapshot) |
| `letters/letter_detection_<id>.pdf` | Generated authority letters (one per approved detection) |
| `smartsurround.db` | SQLite database: detections (+ Track B additive columns/tables); not code |
| `.env` | Live secrets (gitignored): `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `EMAIL_TEST_RECIPIENT`, `ADMIN_PIN`, `EMAIL_TEST_MODE=false`; loaded at import by `python-dotenv` |
| `.env.example` | Documented env surface, placeholders only: `EMAIL_*`, `CORROBORATION_*`; all optional, safe defaults |
| `requirements.txt` | `ultralytics`, Flask stack, `python-dotenv`, `resend>=2.43.0` (loaded at import in `app.py`) |
| `static/utils.js` | Zero-dep client crypto: salted SHA-256 pin digest, UUIDs, entropy, HIBP breach check, latency |

---

## 4. Docs / tooling

| Path | Does what |
|---|---|
| `docs/superpowers/specs/2026-09-16-corroboration-authority-email-design.md` | Track B design spec |
| `docs/superpowers/plans/2026-09-16-corroboration-authority-email.md` | Track B implementation plan |
| `docs/superpowers/plans/2026-09-16-trackb-email-wrap.md` | Session wrap/acceptance doc |
| `docs/TRACKB_WORKLOG.md` | Short worklog + test commands |
| `skills/{brainstorming,frontend-design,writing-plans}/` | opencode skill packs (authoring guidance, not runtime) |
| `venv/` | Project virtualenv (Python 3.13), `venv/Scripts/python` is the launcher |

---

## 5. Known NOT-done / planned (documented for completeness)

- **Real email for third-party authorities** — live send to the account owner is verified; emailing *other* authorities requires a Resend **verified custom domain** (`RESEND_FROM_EMAIL="SmartSurround <notices@yourdomain>"`).
- **Browser/visual letter review + admin letter queue human pass** — not machine-testable this session.
- Cleanup already applied: all session probe/gate `.py` artifacts removed; project root now contains exactly the 7 real modules.

---

*Snapshot generated 2026-09-16. Every functional claim above was boolean-verified against the real on-disk modules (compile lock + live boot/HTTP gates), never from echoed content.*
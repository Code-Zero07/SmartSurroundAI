# SmartSurround — Extensible Hazard Detection & Reporting Workflow — Design Spec

**Date:** 2026-09-18
**Status:** Reviewed & approved in design dialogue (commit pending)
**Scope:** Generalized hazard detection workflow: citizen report → generic incident → geo-clustering → admin approval → authority email. Reuses the existing Flask/SQLite/YOLOv8 codebase; does **not** rewrite it. Waterlogging is a design target but is **not** implemented.

## 1. Goal

Deliver an extensible hazard-reporting workflow around a **generic incident/detection** concept. The current road-damage model remains the only production detector; future models (e.g. waterlogging) must plug in without rewriting location, reporting, PDF, clustering, admin, authority-routing, or email logic.

## 2. Non-goals (explicit)

- No production waterlogging model, UI, or fake detection results.
- No minimum-report counts, corroboration thresholds, or auto-email triggers.
- No rewrite of the app; no unrelated UI/arch changes; no `pothole_reports`/`waterlogging_reports`-style split tables.
- ESP32/direct `/upload` path preserved (updated to generic fields + clustering).

## 3. Architectural principles

1. **lat/lon = authoritative coordinates;** **coarse grid key = authoritative routing key;** **location_name = citizen-editable display name;** **authority_area = server-derived human-readable label.** The citizen may edit `location_name`, never `authority_area` or routing.
2. **Detection model → normalized hazard result → generic incident/report.** No `if pothole:` in the workflow.
3. **Clustering = organization; admin approval = authorization to send; citizen submission ≠ email.**
4. **One valid report can form a cluster.** Representative = highest-confidence eligible report → **one** authority email.
5. **Terminal clusters never reopen:** an approved/sent or settled cluster is terminal and never auto-reopened; new same-area reports start a new cluster generation.

## 4. Architecture overview

```
            IMAGE
              ↓
      DetectionService (hazard.py)
              ↓
  RoadDamageModel (adapter over detector.py)
              ↓
      DetectionResult (normalized)
    hazard_category / hazard_type / confidence / model
              ↓
      Validation (per-category threshold, configurable)
              ↓
   Report (detections row: draft → pending)
              ↓
   Clustering (corroboration.py repurposed; fine area + hazard_category)
              ↓
   Admin cluster review → Approve → route → generate PDF → send email (→ SENT)
```

## 5. Generic detection layer (`hazard.py`, new)

**Contract:**
```
DetectionResult:
  detected: bool
  hazard_category: str   # "road_damage"
  hazard_type: str|None  # "pothole"
  confidence: float
  model: str
  model_version: str|None
```

- `HazardModel` interface: `detect(image_path) -> DetectionResult`.
- `RoadDamageModel`: adapter over existing `detector.analyze_road()`. `detector.py` is **not modified**. Maps damage class → `hazard_type`, exposes `model="road_damage_yolov8s"`. Severity stays internal (no consumer needs it yet).
- `DetectionService`: registry `hazard_category -> model`, built from env `HAZARD_CATEGORIES` (default `road_damage`). `detect(category, path)` selects **only registered models**; unregistered category → clean error. Model errors are caught and soft-failed cleanly (no detector exceptions leak into routes).
- `validate(result)`: per-category minimum-confidence thresholds centralized here, env-configurable (reuse `ROAD_DAMAGE_CONF_THRESHOLD` default for road_damage). Thresholds are for individual-detection validity **only** — never for clustering or email authorization.
- Purpose-built test seam: a **test-only** stub `HazardModel` may be registered in test registries to prove category-agnosticism. No production waterlogging code.
- **Future extension:** real `WaterloggingModel` registers under `hazard_category="waterlogging"`; no workflow changes required.

## 6. Reverse geocoding (`geocode.py`, new; stdlib `urllib`, no new dependency)

- Endpoint is environment-configurable: **`GEOCODE_URL`** (default: the public Nominatim endpoint `https://nominatim.openstreetmap.org/reverse`). The same `geocode.py` code path serves both public Nominatim and a self-hosted instance. The endpoint configuration is **server-side only** and is never exposed to the citizen/frontend.
- `reverse_geocode(lat, lon)` → `{location_name, authority_area}` with a custom `User-Agent` header and a configurable timeout. Fail-open for the **label only**:
  - `location_name` unavailable → client falls back to manual entry.
  - `authority_area` label unavailable → deterministic fallback derived from the coarse key (e.g. `"Area 22.71:88.42"`).
  - Routing key is pure coordinate math (`coarse_area_key`) and is **always** derivable → a geocoder outage **never blocks submission** and never affects routing correctness.

## 7. Citizen flow & public endpoints

All public, all protected by the existing single-use upload-token pattern; all inputs re-validated server-side (coords, image type/size, hazard_category).

1. `GET /api/geocode?lat=&lon=` — reverse geocode; returns normalized `{location_name, authority_area}`.
2. `POST /api/detect` — multipart `image, lat, lon, hazard_category`. Runs `DetectionService` + validation. Returns normalized detection JSON + `valid`. **Stateless: no DB write, no email. Unsupported category → clean 4xx.**
3. `POST /api/report/preview` — same inputs + `location_name`. Server **re-runs detection (authoritative)**, ignores client confidence. If valid: saves image, creates **draft** (`status='draft'`), stores coords/location_name/origin fields/area keys, generates incident PDF via generalized `letter_generator`, returns `{report_id, pdf_url, detection, authority_area}`.
4. `POST /api/report/<draft_id>/submit` — finalizes the **stored draft only** (client sends no detection/coords/location). Refreshes area keys from stored coords, joins/creates its cluster, recomputes representative. **Does NOT email.** → `pending`.
5. Draft hygiene: TTL sweep deletes abandoned drafts and their temporary assets.

Draft immutability: once created, the draft's detection/coords/category are server-authoritative; any change requires backing up and re-detecting (creating a new draft). No partial-modification path.

## 8. Data model (`db.py`, guarded additive migrations)

**`detections` (generic incident/report — table reused):** add
- `hazard_category TEXT NOT NULL DEFAULT 'road_damage'` — backfilled from legacy `hazard_type` (which currently stores `'road_damage'`) **before** constraints, preserving correct categories from legacy values rather than blindly forcing `road_damage`.
- `hazard_type TEXT` — redefined as **specific type** (`pothole`, `waterlogged_road`); legacy rows backfilled from `damage_class`. `damage_class` retained as legacy/origin field.
- `location_name TEXT`, `authority_area TEXT`, `detection_model TEXT`, `detection_model_version TEXT`, `cluster_id INTEGER`.
- `status` values extended with `draft`. Report lifecycle: `draft → pending → approved/rejected` (rejected reports never become representatives).

**`corroboration_clusters` (repurposed → admin cluster table; table rebuild — 2 legacy rows):**
- Grouping key moves to `(corroboration_area_key, hazard_category)`; the UNIQUE constraint is **relaxed** to allow multiple rows per key (generations).
- Adds `representative_report_id INTEGER`, `authority_area_key TEXT`, `updated_at TEXT`; drops `window_start`.
- `lifecycle` reused (no competing status system) with **cluster lifecycle**: `pending_approval → approved → sent`; exceptions: `missing_authority_email`, terminal `settled`.

**`authorities` (minimal):**
- Optional `authority_name TEXT`.
- **Semantic note (documented):** `authorities.hazard_type` retains the **category key** (`'road_damage'`) for compatibility — it is *not* the normalized specific type. Lookups use `authority_area_key (coarse) + hazard_category`. The `detections.hazard_type` column carries the specific type; the two are intentionally different.

## 9. Clustering service (`corroboration.py` repurposed in place)

- Removed: `CORROBORATION_THRESHOLD`, windows, derived counts, `offending_clusters`, auto-send sweep. Retained: `fine_area_key` (~111m), `coarse_area_key` (~1.1km).
- A report joins the **active** cluster (`lifecycle IN pending_approval/approved/missing_authority_email`) for `(fine key, hazard_category)`; else creates a new `pending_approval` cluster.
- **Never reopened after `sent`/`settled`:** new same-area/hazard reports start a new cluster generation (only one active per key enforced in code). A later send is a new cluster id → a new email, not a duplicate.
- **Representative:** highest-confidence report with `status IN (pending, approved)` in cluster. Recompute on (a) join, (b) member status change via per-report approve/reject; a freshly rejected representative is replaced by the next-highest eligible report.
- Cluster `authority_area_key` refreshes from the representative's **stored coords** at recompute time (routing never uses `location_name`).
- `POST /upload` (ESP32/direct) updated: inserts `pending` report with full generic fields, runs clustering, **no email**.

## 10. Admin

**View:** cluster cards (`authority_area` label + coarse grid key, hazard category, report count, representative thumb/confidence/type, status badge, `last_send_error` when present) with "▼ View N other submissions" — every valid photo inspectable; rejected marked.

**Routes (all `@require_auth`):**
- `POST /admin/cluster/<id>/approve` — from `pending_approval` only. Sets `approved` (independent of send success), then routes: verified authority + email → generate final PDF → send → `sent`; missing/pending/bounced authority → `missing_authority_email`; send failure → stays `approved`, records `last_send_error`, retryable.
- `POST /admin/cluster/<id>/send` — retry-safe finalizer: only from `approved`/`missing_authority_email` **and** `sent_at IS NULL`; regenerates PDF, sends, marks `sent`; stores `last_send_error` on failure.
- `POST /admin/cluster/<id>/settle` — explicit terminal close, no send.
- `POST /admin/cluster/<id>/authority-email` — recovery **only** from missing/invalid-email state; validates email; `get_or_create_authority(coarse key, hazard_category, email)` marked **verified** (admin-confirmed) then attempts send. Reused by future reports in that area.
- Existing `/admin/approve/<id>`, `/admin/reject/<id>` retained for per-report review; they recompute the representative and **never** trigger email.

**Guards:** `approve`/`send` reject `sent`/`settled`; `sent` and `settled` are terminal and idempotent; a settled/sent cluster is never silently reopened by later reports.

## 11. Authority routing & email

- Lookup: **coarse grid key + `hazard_category`**, server-side from representative coords. Never `location_name`.
- Email states tracked distinctly: missing email / admin-confirmed email / previous bounce / send-or-provider failure. **SMTP/provider acceptance is not treated as proof of delivery** (delivery/bounce status tracked separately).
- Send: reuse `email_driver.send_authority_email()`; generalized `letter_generator.generate_letter()` consumes normalized fields (`hazard_category`, `hazard_type`, `confidence`, `location_name`/coords, timestamp, image). **One** email, **PDF letter only** (representative photo embedded), body/subject generic over hazard category. No pothole-specific logic.
- `sent` terminal: guarded by `sent_at IS NULL` + lifecycle check; `sent_at` written only on success; failure leaves it null.

## 12. Frontend

**Citizen wizard (`index.html` + new `static/citizen.js`, Leaflet/OSM CDN, vanilla JS, inline styles):**
1. Location — Geolocation auto-fill → geocode → editable `location_name` + read-only `authority_area`; Leaflet draggable marker re-geocodes on change; manual fields retained. Nominatim outage → manual `location_name`, key-derived `authority_area` fallback; submission is never hard-blocked.
2. Photo — existing camera/upload reused.
3. Detection — generic Hazard / Type / Confidence card; blocked if `valid:false`.
4. Generate & Preview — `POST /api/report/preview` → draft + in-page PDF preview.
5. Submit — `POST /api/report/<draft_id>/submit` (no client detection/coords sent); success = "Report submitted for review", **no email mention**.

**Admin UI (`admin.html`):** cluster cards + state actions as §10; authority registry, verify/bounce, test-email retained.

## 13. Security / backend validation

Server must validate: latitude/longitude bounds; image type/size; `hazard_category` ∈ registry; detection confidence/results (via authoritative re-run); report & cluster state; authority email format. Reject client-supplied `location_name` used for routing, and client-supplied `authority_area`, `confidence`, `cluster_id`, and `status`. Tokens + `@require_auth` applied per §7/§10. Secrets stay in `.env` (untracked).

## 14. Environment / configuration

Documented env surface (additive to existing):
- `HAZARD_CATEGORIES` — registered hazard categories (default `road_damage`).
- `GEOCODE_URL` — reverse-geocoding endpoint (default: public Nominatim `https://nominatim.openstreetmap.org/reverse`); server-side only, never exposed to clients.
- Per-category validation thresholds, e.g. `ROAD_DAMAGE_MIN_CONF` (reusing existing defaults where appropriate), centralized in `hazard.py`.
- Draft TTL (default e.g. 24h) for the abandoned-draft sweep.

## 15. Testing (unittest, mirrors existing `tests/`)

- **Detection contract**: normalized result from registered model; unregistered category → clean error; adapter over mocked `analyze_road`; per-category validation; soft-failed model errors.
- **Geocode**: success/failure; label fallback; coarse-key determinism; configurable endpoint respected.
- **Report flow**: `/api/detect` stateless; preview re-detects + drafts only when valid; submit finalizes stored draft; no email on submit; TTL cleanup; token gating; input validation.
- **Clustering**: first-report cluster; join active; new generation after `sent`/`settled`; rejected ineligible; highest-confidence representative; recompute on join/review; single-report cluster.
- **Admin/cluster**: approve from `pending_approval` only; reject `sent`/`settled`; `approved` independent of send; `/send` resumable + idempotent (`sent_at`); `settled` terminal; authority-email recovery gated + state-restricted; per-report review never emails.
- **Authority/email**: coarse-key + hazard_category lookup; missing → `missing_authority_email`; admin-confirmed email verified; bounce tracked; acceptance ≠ delivery; one generic email, PDF only, representative photo only.
- **Category-agnostic proof**: test-only stub `HazardModel` runs report→cluster→approve→email end-to-end.
- **Existing tests**: threshold/auto-send `test_email_workflow.py` tests removed/rewritten; `test_auth_protection.py` extended for new admin routes.

## 16. Out of scope / future

- Real waterlogging detection model (extension point only, per §5).
- Reverse-geocoding provider swap beyond the configurable `GEOCODE_URL` endpoint; production auth; deployment.
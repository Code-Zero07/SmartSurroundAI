# Corroboration → Authority Email Implementation Plan (Track B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the corroboration engine + authority email lifecycle to the existing SmartSurround Flask backend without touching Track A.

**Architecture:** Additive schema (new nullable columns on `detections`, two new tables), a `corroboration.py` module that *derives* clusters and counts (never stores a counter), an `authority_routing.py` module for the pending/verified/bounced email lifecycle, an `email_driver.py` SMTP sender with `EMAIL_TEST_MODE`/`EMAIL_DRIVER` switches, and admin routes/templates for typing + verifying authority emails and marking bounces. `app.py` inserts call the engine's sweep at touchpoints.

**Tech Stack:** Flask (existing), Python stdlib `sqlite3`, `smtplib` (stdlib — the only outbound driver, no new deps), reportlab (existing, letter PDF is the email attachment), `os.environ` config (no dotenv; requires `.env.example` guidance only).

**Spec:** `docs/superpowers/specs/2026-09-16-corroboration-authority-email-design.md` (approved, sealed).

## Global Constraints

- **Track A frozen:** do NOT modify `detector.py`, `letter_generator.py`'s public interface, the Track A parts of `app.py`, or the ESP32-CAM / citizen `index.html`+`static/app.js` camera+geolocation work. `/upload` image/source/lat/lon/description field names unchanged, Track A admin verification flow unchanged.
- **No git, no commits:** this folder is not a git repo; there is no git step anywhere in this plan.
- **No new citizen-facing fields:** `client_ip` is captured server-side from `request.headers` (via `DB` `X-Forwarded-For` first hop or `request.remote_addr`), never a submitted form field. Corroboration and authority area keys are **derived** from the already-submitted lat/lon.
- **Derived corroboration, no stored counter:** corroboration count = `COUNT(DISTINCT client_ip)` pulled from `detections`; there is NO stored corroboration counter column, matching the approved spec correction. Never incorrect — it is the rows.
- **No sender to citizens, ever:** only auto-email to *verified* authority addresses. Everything unverified routes to the admin queue (tagged), exactly like today's Track A pending review.
- **Persistence of authority state only:** `authorities` table stores (coarse key, hazard_type, email, status pending/verified, created_at, verified_at, last_bounced_at). `corroboration_clusters` table stores cluster lifecycle state (open/corroborated/emailed/settled) + window start/end + emailed_at + letter_path. Counts are never table columns.
- **Test mode default on:** `EMAIL_TEST_MODE=true` (send to `EMAIL_TEST_RECIPIENT` default `codexzero98@gmail.com`), `EMAIL_DRIVER=smtplib` default, `CORROBORATION_THRESHOLD` default 4, `CORROBORATION_WINDOW_DAYS` default 14.
- **Bounce = manual admin action:** flips verified → pending (sets bounced_at); next corroboration re-routes to admin. No webhook, no scheduler.
- **Manual verification only:** the repo has no test harness. Every task ends with concrete manual checks; the final task runs the full Track B acceptance matrix.

---

### Task 1: Additive schema in `db.py`

**Files:**
- Modify: `db.py` (add columns to `detections`, add `CREATE TABLE` block for `authorities` + `corroboration_authority_window`... name: `corroboration_clusters`)
- Create: none (schema is created by existing `init_db()`)

**Interfaces:**
- Consumes: existing `get_conn()`.
- Produces: `detections` gains nullable columns (`client_ip TEXT`, `corroboration_area_key TEXT`, `authority_area_key TEXT`, `hazard_type TEXT NOT NULL DEFAULT 'road_damage'`); new tables `authorities` and `corroboration_clusters`; new helpers `insert_authority(...)`, `list_authorities(area_key=None)`, `get_authority(area_key,hazard_type)`, `set_authority_status(...)`, `upsert_cluster(...)`, `get_open_cluster(area_key,hazard_type)`, `list_clusters()`, `mark_cluster(...)`.

- [ ] **Step 1: Add the three columns to the `detections` CREATE TABLE**

In `db.py` `init_db()`, add to the existing `CREATE TABLE IF NOT EXISTS detections`:

```python
client_ip TEXT,                       -- server-captured, corroboration IP dedup
hazard_type TEXT NOT NULL DEFAULT 'road_damage',
corroboration_area_key TEXT,          -- round(lat,3),round(lon,3)  ~111 m
authority_area_key TEXT,              -- round(lat,2),round(lon,2)  ~1.1 km coarse
```

- [ ] **Step 2: Add the two new tables to `init_db()`**

```sql
CREATE TABLE IF NOT EXISTS authorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    authority_area_key TEXT NOT NULL,   -- coarse key, ~1.1 km
    hazard_type TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/verified
    created_at TEXT NOT NULL,
    verified_at TEXT,
    last_bounced_at TEXT,
    UNIQUE (authority_area_key, hazard_type)
);

CREATE TABLE IF NOT EXISTS corroboration_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_key TEXT NOT NULL,             -- fine corroboration key, ~111 m
    hazard_type TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,       -- window_started_at + WINDOW_DAYS
    status TEXT NOT NULL DEFAULT 'open',-- open/corroborated/emailed/settled
    corroborated_at TEXT,
    emailed_at TEXT,
    letter_path TEXT,
    sent_to TEXT,                       -- authority email actually sent to
    UNIQUE (area_key, hazard_type, window_started_at)
);
```

- [ ] **Step 3: Add the db helpers (insert/list/get/mark)**

Implement in `db.py`:

```python
def insert_authority(authority_area_key, hazard_type, email, status="pending"):
def list_authorities(authority_area_key=None):
def get_authority(authority_area_key, hazard_type):
def set_authority_status(authority_id, status):
def upsert_cluster(area_key, hazard_type, window_start, window_end, status="open"):
def get_open_cluster(area_key, hazard_type):
def list_clusters():
def mark_cluster(cluster_id, status, emailed_at=None, letter_path=None, sent_to=None):
```

Each follows the existing `get_conn()` / `conn.execute` / `conn.commit()` / `conn.close()` pattern. UNIQUE constraints make `get_open_cluster` derivable (the one open row per area+hazard).

- [ ] **Step 4: verify schema via PRAGMA + a row round-trip**

Run (PowerShell, from the project dir, after momentarily seeding via the app):

```powershell
python -c "import db; db.init_db(); c=db.get_conn(); print([r[1] for r in c.execute('PRAGMA table_info(detections)')]); print([r[1] for r in c.execute('PRAGMA table_info(authorities)')]); print([r[1] for r in c.execute('PRAGMA table_info(corroboration_clusters)')])"
```

Expected: existing columns PLUS the 4 new ones; and both new tables' column lists print.

---

### Task 2: Area-key derivation + corroboration engine in `corroboration.py`

**Files:**
- Create: `corroboration.py`

**Interfaces:**
- Consumes: `db` (rows), env `CORROBORATION_THRESHOLD` (default 4), `CORROBORATION_WINDOW_DAYS` (default 14).
- Produces: `derive_area_keys(lat, lon) -> dict(fine_key, coarse_key)`; `client_ip_from(request) -> str`; `count_corroborators(area_key, hazard_type) -> int`; `sweep(when=None)`; `escalate(...)`.

- [ ] **Step 1: env consts + area key derivation**

```python
CORROBORATION_THRESHOLD = int(os.environ.get("CORROBORATION_THRESHOLD", "4"))
CORROBORATION_WINDOW_DAYS = int(os.environ.get("CORROBORATION_WINDOW_DAYS", "14"))

def derive_area_keys(lat, lon):
    """lat/lon each rounded to get both keys from ONE insert."""
    return {
        "fine_key": f"{round(lat,3)},{round(lon,3)}",   # ~111 m
        "coarse_key": f"{round(lat,2)},{round(lon,2)}", # ~1.1 km
    }
```

- [ ] **Step 2: IP capture (proxied first hop, never a form field)**

```python
def client_ip_from(request):
    """X-Forwarded-For first hop when present, else remote_addr."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or None
```

- [ ] **Step 3: derived corroboration count**

```python
def count_corroborators(area_key, hazard_type, window_started_at):
    return db.count_distinct_ips(area_key, hazard_type, window_started_at)
```

Add to `db.py`: `count_distinct_ips(area_key, hazard_type, window_started_at)` implementing exactly:

```sql
SELECT COUNT(DISTINCT client_ip)
FROM detections
WHERE hazard_type = :hazard_type
  AND corroboration_area_key = :area_key
  AND status IN ('pending','approved','letter_sent')
  AND client_ip IS NOT NULL
  AND created_at >= :window_started_at
  AND created_at <  :window_started_at + window
```

(The approved note: `letter_sent` rows still corroborate; they represent real corroboration that was already acted on. `rejected` never does.)

- [ ] **Step 4: lazy window sweep at touchpoints**

```python
def sweep():
    """Derive each open cluster's count; escalate at threshold; settle expired windows."""
```

Rules per the spec: a cluster whose window expired below threshold → `settled` (dissolves; its reports stay in Track A pending queue — no letter). A cluster at/above threshold → `corroborated` → route (Task 3). Root clusters already corroborated keep accepting in-window joins without re-sending (spec §4).

- [ ] **Step 5: wrapper for app.py**

```python
def do_corroboration_sweep_on_upload(new_id):
    """Insert hook: re-derive the cluster for this report, then sweep."""
```

- [ ] **Step 6: manual check**

From the REPL with a temp citizen row, `import corroboration; corroboration.sweep()` must not raise and must roll a cluster open. (Full acceptance is Task 7.)

---

### Task 3: Authority routing + lifecycle in `authority_routing.py`

**Files:**
- Create: `authority_routing.py`

**Interfaces:**
- Consumes: `db` authorities rows, env `EMAIL_TEST_RECIPIENT` (default `codexzero98@gmail.com`).
- Produces: `route(area_coarse_key, hazard_type) -> None|verified-email|'admin'`; `mark_bounced(authority_id)`; `verify_authority(authority_id)`.

- [ ] **Step 1: routing matrix (the approved table)**

```python
def route(coarse_key, hazard_type):
    row = db.get_authority(coarse_key, hazard_type)
    if row is None or row["status"] != "verified":
        return "admin"
    return row["email"]
```

- [ ] **Step 2: lifecycle flips**

```python
def verify_authority(authority_id):
    db.set_authority_status(authority_id, "verified")
def mark_bounced(authority_id):
    db.set_authority_status(authority_id, "pending", bounced=True)  # verified->pending
```

- [ ] **Step 3: manual check** — seed via REPL, route returns `"admin"` for no-row and for `pending`, returns the email for `verified`.

---

### Task 4: Outbound email with test mode in `email_driver.py`

**Files:**
- Create: `email_driver.py`

**Interfaces:**
- Consumes: env `EMAIL_DRIVER` (default `smtplib`), `EMAIL_TEST_MODE` (default `true`), `EMAIL_TEST_RECIPIENT` (default `codexzero98@gmail.com`), `EMAIL_SMTP_*` vars (optional).
- Produces: `send_email(subject, body_html, to_recipient, attachment_path=None, attachment_name=None) -> bool`.

- [ ] **Step 1: driver switch**

```python
def send_email(subject, body_html, to_recipient, attachment_path=None, attachment_name=None):
    if EMAIL_DRIVER == "smtplib":
        return _send_smtplib(...)
    raise ValueError(f"Unknown EMAIL_DRIVER: {EMAIL_DRIVER}")
```

- [ ] **Step 2: test mode**

```python
def _send_smtplib(...):
    if EMAIL_TEST_MODE:
        to = EMAIL_TEST_RECIPIENT  # always rewritten in test mode
    if not EMAIL_SMTP_HOST or not EMAIL_SMTP_USER:
        log(f"[EMAIL:TEST] would send to {to} — no SMTP creds configured")
        return True   # test mode + no creds = logged, never crashes
    ...real smtplib.SMTP(...) send with MIMEMultipart + PDF attach...
```

- [ ] **Step 3: manual check** — `send_email(...)` with no creds logs "would send" and returns True without raising; with a `EMAIL_TEST_MODE=false` and no creds it returns False + a logged reason (never crashes the request).

---

### Task 5: Wire corroboration + authority email into `app.py` + admin UI

**Files:**
- Modify: `app.py`, `templates/admin.html` (additive sections)

**Interfaces:**
- Consumes: `corroboration`, `authority_routing`, `email_driver`, `db`.
- Produces: `/upload` records `client_ip` + derived keys; corroboration sweep runs at insert; corroborated+verified cluster → `email_driver.send_email(letter_pdf)`; `/admin` shows authority rows (type-email, verify, mark-bounced forms); admin approve still generates Track A letter.

- [ ] **Step 1: `/upload` captures IP + derives keys + tags hazard**

In `upload()`: `client_ip = corroboration.client_ip_from(request)`; derive `fine_key`/`coarse_key` from lat/lon when present; pass into `db.insert_detection(...)`.

- [ ] **Step 2: sweep on insert**

After insert, `corroboration.do_corroboration_sweep_on_upload(new_id)`. On corroboration: `route = authority_routing.route(coarse_key, hazard_type)`; if a verified email → `email_driver.send_email(..., attachment=letter_pdf, to=route)` and mark cluster emailed; else leave in admin queue (Track A prevails).

- [ ] **Step 3: admin surface**

Add to `admin.html` an "Authority emails" section: pending rows with a **type/verify** form and a **mark bounced** button for verified rows. Add routes `/admin/authority/verify/<id>` and `/admin/authority/bounce/<id>`; keep `/admin/approve`, `/admin/reject` byte-identical.

- [ ] **Step 4: manual checks**

`/upload` as citizen at identical lat/lon 4× from (simulated) 4 distinct IPs → letter emailed to `codexzero98@gmail.com` (test mode); same IP 4× → no email; admin verify + bounce → next corroboration routes to admin again.

---

### Task 6: README, `.env.example` guidance, Track A regression

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** Document env vars (`CORROBORATION_THRESHOLD`, `CORROBORATION_WINDOW_DAYS`, `EMAIL_TEST_MODE`, `EMAIL_TEST_RECIPIENT=codexzero98@gmail.com`, `EMAIL_DRIVER`, `EMAIL_SMTP_*`), the test-mode acceptance steps, and a "Track A unchanged" note.
- [ ] **Step 2:** Full manual acceptance run (Task 1 Step 4 + Task 7 matrix below) + confirm `/upload`, `/admin`, approve/reject, letter download all still work (Track A regression).

### Task 7: Acceptance matrix (manual)

| # | Scenario | Expected |
|---|---|---|
| 1 | First citizen report, brand-new area | No email; pending; admin queue unchanged |
| 2 | 4 distinct IPs same fine cluster within window | Corroborated → email to verified authority / test recipient; cluster emailed |
| 3 | Same IP 4× | Corroboration count stays 1 (dedup); no email |
| 4 | Rejected report | Never corroborates |
| 5 | New report into already-emailed cluster (window open) | Joins, counts, no re-send |
| 6 | Window elapses below threshold | Cluster settles; no email; reports stay in Track A pending |
| 7 | Bounce: admin marks verified → pending | Next corroboration re-routes to admin |
| 8 | Test mode no creds | Send logged "would send", request not crashed |
| 9 | Track A regression | `/upload`, `/admin`, approve/reject, letter link all intact |

Each row = a concrete manual step in the README.

**Self-review checklist (run at end):** every spec §route decision has a task; no stored corroboration counter exists anywhere; `EMAIL_TEST_MODE` default true; bounce is manual-only; Track A files untouched (grep `git status`-equivalent: confirm no edits to `detector.py`/`letter_generator.py`/Track A index/app.js).

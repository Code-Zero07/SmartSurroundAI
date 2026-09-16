# SmartSurround — Corroboration → Authority Email (Track B) Design

> **Date:** 2026-09-16
> **Status:** Approved section-by-section in-conversation (Sections 1–4 +
> the two-key amendment + the correction that corroboration is **derived**,
> not stored). This file is the clean canonical render of that approved
> design; the implementation plan argues from it.
>
> **Implements:** the "Citizen complaint portal + GPS + corroboration →
> authority email" roadmap item, without touching Track A (ESP32-CAM
> upload, admin review, letter generation) or the frozen `/upload` form.
>
> **Plan:** `docs/superpowers/plans/2026-09-16-corroboration-authority-email.md`

## Terminology

| Term | Meaning |
|---|---|
| **fine key** (`corroboration_area_key`) | `round(lat, 3), round(lon, 3)` — ~111 m cells. Used to find *corroborating* reports (Track B). |
| **coarse key** (`authority_area_key`) | `round(lat, 2), round(lon, 2)` — ~1.1 km cells. Used to route to the *authority* that owns the jurisdiction. |
| **hazard_type** | The kind of hazard, e.g. `road_damage`. Track B hazard filter; corroboration and authority routing group by it. |
| **corroboration count** | **Derived**, never stored. `COUNT(DISTINCT client_ip)` over qualifying reports sharing (fine key + hazard_type + open window). |
| **corroborated** | Derived count ≥ `CORROBORATION_THRESHOLD`. |
| **emitted / emailed** | Corroborated cluster whose letter was auto-sent to the authority email. |
| **authority** | One row per `(authority_area_key, hazard_type)`; the verified email that corroborated clusters route to. |
| **window** | Corroboration window; reports only corroborate within `CORROBORATION_WINDOW_DAYS` of the cluster's first report. |

## One-sentence pitch

Citizen photos of the same road damage, reported by **different people**
(same fine area + same hazard within a window) corroborate each other;
when enough distinct people agree, the cluster escalates to a **verified
authority email** with the generated letter PDF attached — explained fully
by exactly one design decision per section below.

## Global constraints

- **Track A byte-identical.** Detect/analyze/letter/`app.py` admin flow,
  `/upload` field names (`image, source, lat, lon, description`) and
  index form unchanged. No new citizen-facing fields. No new stored
  corroboration counter.
- **No git repo, no commits.** No `.git`; implement + document + verify.
- **Smtplib-only for now** (stdlib, zero new deps) with an
  `EMAIL_DRIVER` switch for a future transactional API. `EMAIL_TEST_MODE`
  defaults **true**; the send path still runs end-to-end but rewrites
  `To:` to `EMAIL_TEST_RECIPIENT` (default `codexzero98@gmail.com`) and
  logs the would-be send. `EMAIL_TEST_MODE=false` requires SMTP creds or
  else skips with a log line — nothing ever crashes the request.
- **Bounce = manual admin action.** An admin flips a verified authority
  back to pending; the next corroboration re-routes to admin (no auto
  re-send, no webhook, no scheduler). Matches the Track B acceptance
  "simulating a bounce flips verified → pending → next corroboration
  routes to the re-typed one."
- **Citizen form untouched.** The seeded test + the tracked-address test
  address are configured via env, never by the reporting citizen.

---

## Section 1 — Schema (additive, migration-safe)

Two new columns on `detections` plus two new tables. All additive —
existing rows and Track A queries keep working.

### `detections` (additive)

| Column | Notes |
|---|---|
| `client_ip` TEXT | The reporting client's IPv4/IPv6 (from `X-Forwarded-For` first hop or `request.remote_addr`). Used for corroboration **dedup** (COUNT DISTINCT). NULL for legacy rows. |
| `hazard_type` TEXT NOT NULL DEFAULT `'road_damage'` | What kind of hazard this report is about. Corroboration + authority routing group by it. |
| `corroboration_area_key` TEXT | Fine key derived at insert from `lat/lon` if present: `round(lat,3),round(lon,3)`. |
| `authority_area_key` TEXT | Coarse key derived at insert from `lat/lon` if present: `round(lat,2),round(lon,2)`. |

Derived at insert from the citizen's own GPS (same rule as corroboration
area mapping — no new form field, both keys fall out of the existing
`lat`/`lon`).

### `authorities` table

```sql
CREATE TABLE IF NOT EXISTS authorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    authority_area_key TEXT NOT NULL,    -- coarse key
    hazard_type TEXT NOT NULL,           -- e.g. 'road_damage'
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / verified / bounced
    created_at TEXT NOT NULL,
    verified_at TEXT,
    bounced_at TEXT,
    UNIQUE (authority_area_key, hazard_type)
)
```

Lifecycle:

```
none -> pending -> verified -> (bounce, manual) -> pending -> ...
            |                        |
            +-- admin types the      +-- admin marks "bounced" on an
                email at review          already-verified address; next
                time / first-ever        corroboration re-routes to admin
```

- **`none`**: no email on file for that `(authority_area_key, hazard_type)`.
- **`pending`**: an email was typed but not yet verified. Corroboration in
  this area does **not** auto-send; it routes to the **admin queue** tagged
  "authority email pending." Admin verifies → becomes verified.
- **`verified`**: corroborated clusters in this coarse area auto-email the
  letter PDF to this address.
- **Bounce (manual, Track-B acceptance):** admin flips a `verified` address
  to pending (`status='bounced'` in the row is allowed but practically we
  reuse pending + `bounced_at`). The **next** corroboration in that area
  routes to admin again; admin re-verifies or re-types → verified → sends.

### `corroboration_clusters` table

```sql
CREATE TABLE IF NOT EXISTS corroboration_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corroboration_area_key TEXT NOT NULL,   -- fine key
    hazard_type TEXT NOT NULL,
    window_starts_at TEXT NOT NULL,          -- UTC iso of first report
    window_ends_at TEXT NOT NULL,            -- + CORROBORATION_WINDOW_DAYS
    state TEXT NOT NULL DEFAULT 'open',      -- open / corroborated / emailed / settled
    corroborated_at TEXT,
    emailed_at TEXT,
    letter_path TEXT,
    UNIQUE (corroboration_area_key, hazard_type)  -- one cluster per fine area+hazard
)
```

**There is no stored `corroboration_count` column.** The count is always
computed by query. This table only stores *lifecycle state* that can't be
re-derived (whether it was already emailed), keyed to the fine area.

---

## Section 2 — Corroboration engine (derived, no stored counter)

Per the approved correction: **corroboration is derived, never stored.**
No counter column, no IP list table. The number is always:

```sql
SELECT COUNT(DISTINCT client_ip)
FROM detections
WHERE hazard_type = :hazard_type
  AND corroboration_area_key = :fine_key
  AND status IN ('pending', 'approved')          -- accepted corroborators
  AND client_ip IS NOT NULL
  AND created_at >= :window_starts_at
  AND created_at <  :window_ends_at
```

- **Dedup is the point:** `COUNT(DISTINCT client_ip)` — one citizen
  uploading the same spot 5× counts onceches. IPs are captured at insert
  (`client_ip`); rows without an IP (legacy) never corroborate.
- **Qualifying corroborators** = `status IN ('pending','approved')`. A
  report that gets `rejected` no longer corroborates (removed from the
  count on the sweep). A report that's already `letter_sent` still counts
  (it was real corroboration). An `emailed` cluster keeps counting new
  IPC-distinct reports as corroboration **without re-sending** — window
  still open, cluster already emailed → joins, counts, no re-send; once
  window elapses a **fresh cluster** can form (see Section on lifecycles).
- **Threshold & window (env):**

  | Env | Default |
  |---|---|
  | `CORROBORATION_THRESHOLD` | `4` (distinct IPs needed to corroborate) |
  | `CORROBORATION_WINDOW_DAYS` | `14` |

- **Cluster lifecycle (lazy sweep, no scheduler):**

  ```
  open -> (count >= THRESHOLD) -> corroborated -> (route) -> emailed
   |-> (window elapsed, never corroborated) -> settled        [no letter]
  ```

  The sweep runs lazily at **every touchpoint** (new report insert, admin
  /admin load, before a corroboration email, anytime `/corroborate` or
  admin routes are hit). On each sweep it re-derives each open cluster's
  count and walks every cluster whose window has elapsed — so a window
  that lapsed while nobody visited does not strand a letter forever.

- **Report into an already-emailed cluster within the open window:**
  joins as corroboration (counts an extra distinct IP), **no re-send**.
  Only a *fresh* cluster (new window after elapse) that reaches threshold
  triggers its own email.

---

## Section 3 — Authority email routing

When a cluster corroborates, find its **coarse** cell holes at insert
(`authority_area_key`) and route:

| authority `(authority_area_key, hazard_type)` | Outcome |
|---|---|
| **none** | No auto-send. Cluster routes to admin queue tagged "authority email needed" — admin types one at review (becomes pending). Re-runs corroboration → sends on verify. |
| **pending** | No auto-send. Cluster routes to admin queue tagged "authority email pending." Admin verifies (or corrects) → verified → sends. |
| **verified** | **Auto-send.** Email the letter PDF to `authorities.email`. Mark cluster `emailed`. |
| **bounced** (verified → manual flip) | Reverts to pending. Next corroboration re-routes to admin; admin re-verifies/re-types → verified → next cluster auto-sends. |

**First report in a brand-new area** always lands in the admin queue for
review (Track A flow unchanged); no letter is auto-sent just because one
citizen filed a claim — that's the whole point of corroboration.

---

## Section 4 — Email outbound (smtplib, test-mode first)

Skill: `email_driver.py` with:

```python
import os
EMAIL_DRIVER = os.environ.get("EMAIL_DRIVER", "smtplib")     # future: "sendgrid"
EMAIL_TEST_MODE = os.environ.get("EMAIL_TEST_MODE", "true").lower() == "true"
EMAIL_TEST_RECIPIENT = os.environ.get("EMAIL_TEST_RECIPIENT", "codexzero98@gmail.com")
def send_email(to_email, subject, body_html, attachment_path=None): ...
```

- `EMAIL_DRIVER=smtplib` → stdlib `smtplib` + `email.mime` with the letter
  PDF attached. `EMAIL_SMTP_*` env vars optional — if unset and
  `EMAIL_TEST_MODE=true`, it **logs** the send instead of connecting
  (nothing crashes without credentials).
- `EMAIL_TEST_MODE=true` → rewrite `To:` to `EMAIL_TEST_RECIPIENT`, keep
  full send path when creds exist, else log. Acceptance: a test send to
  `codexzero98@gmail.com` succeeds (real SMTP) or is logged (no creds).
- `email_driver` is the **single** outbound seam; a future SendGrid driver
  replaces the contents of `send_email` and switches via `EMAIL_DRIVER`.
- Bounce handling is **manual admin action** (this section's acceptance):
  no webhook now; admin marks bounced → next corroboration re-routes.

---

## Section 5 — Acceptance matrix (Track B)

| # | Scenario | Expected |
|---|---|---|
| B1 | First report in a brand-new coarse area | No email; pending in admin queue |
| B2 | `CORROBORATION_THRESHOLD` distinct IPs in one fine cell within window | Cluster corroborates → routes to verified authority → letter emailed (test-mode logged to `codexzero98@gmail.com`) |
| B3 | Same IP uploads 4× into same cluster | Count = 1; no corroboration; no email |
| B4 | Rejected report | Removed from corroboration count; does not join cluster |
| B5 | Report into already-emailed cluster within window | Joins (counts toward dedup), **no re-send**; elapsed window → fresh cluster starts |
| B6 | Window elapses below threshold | Cluster settles; no letter; no email |
| B7 | Bounce simulation: admin marks verified → bounced | Verified → pending; next corroboration re-routes to admin; admin re-types → verified → sends |
| B8 | Track A regression | `/upload` fields, ESP32 post, admin approve/reject, letter PDF download — all unchanged |
| B9 | Test-mode no-creds | Send logged, never crashes the request, recipient is `codexzero98@gmail.com` |
| B10 | Corroboration-sent in test mode | `To:` rewritten; full send/lie path exercised |

---

## Non-goals

- SendGrid/SES transactional driver (only the `EMAIL_DRIVER` switch seam).
- Auto bounce detection (webhooks) — manual flip only (MVP).
- Citizens picking authority addresses, or any new citizen form field.
- Background scheduler/cron — lazy sweep at touchpoints only.
- Changing Track A letter generation, model, or `/upload`.

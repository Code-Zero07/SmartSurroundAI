# SmartSurround Backend — Work Log & Test Plan
**Project:** `D:\Program\New folder\smartsurround_backend02\smartsurround_backend`
**Date:** 2026-09-16 · **Status:** Track A regression-locked, Track B additive, **live Resend email verified**, admin protected

---

## 1. Work done (this session, corruption-proof verified)

### 1.1 Track A — regression-locked (byte-exact contract preserved)
| Surface | Verified (boolean-only probe) |
|---|---|
| `db` six-surface | `insert_detection`, `update_detection_status`, `list_by_status`, `get_detection`, `get_conn`, `list_all` — all callable `True` |
| `detector.analyze_road` | callable `True`; keys `road_condition`, `damage_type`, `confidence`, `accepted` in analyze result |
| `app` boot | `boot_ok True`; Track A routes byte-intact |
| HTTP live | `GET /` = 200, `GET /admin` = 200, `POST /upload` ≠ 500, pending row created |

### 1.2 Track B — additive (integrity preserved to Track A)
| Module | Additive surface | Gate |
|---|---|---|
| `db.py` | `client_ip`, `hazard_type`, `corroboration_area_key`, `authority_area_key` col-rows additive | additive columns present on live row |
| `corroboration.py` | fine/coarse area keys, **derived** corroboration count (never stored), window sweep | `corroboration_count()` int ≥ 0, `run_lifecycle_sweep` callable |
| `authority_routing.py` | cluster lifecycle (pending→verified→bounced→pending flip), route decision | `route_pending_clusters` callable, no-raise on empty |
| `email_driver.py` | `RESEND_API_KEY` / `EMAIL_DRIVER` (resend|smtplib) / `EMAIL_TEST_MODE` env surface | **live send confirmed**: Resend message id `01a0ab0d-60b5-74ac-9b16-c51365229587` → `codexzero98@gmail.com` |

**HTTP workflow gate — email chain:** `upload → row(pending, client_ip, hazard_type, corr_area, auth_area) → run_lifecycle_sweep → route_pending_clusters → send_authority_email` verified end-to-end. Live Resend dispatch confirmed from `onramp@resend.dev` (Resend shared sender — delivers to the account owner only).

### 1.3 Admin login protection + frontend visibility (added after Track B)
- **Auth:** `POST /login/creds` (PIN → salted SHA-256 digest match) issues a 30-min httpOnly `ss_token` cookie + `X-Set-Auth-Token` header; every admin mutation route is `@require_auth` (approve/reject/verify/bounce/authority-add/test-email).
- **Rate limit:** 5 failed PINs per IP → 1-hour lockout (429). CORS exact-origin only. `/upload` requires a token from `GET /upload/token` (hidden `_upload_token` field / query param / `X-Upload-Token` header).
- **Frontend visibility:** `/admin` polls `GET /admin/api/clusters` (JSON) every 10 s; lifecycle/email badges (SENT + `emailed_at`), toasts, authority registration form, "send test notification email" button. Zero-dep client crypto in `static/utils.js`.
- **Tests:** `tests/test_auth_protection.py` + `tests/test_email_workflow.py` — **25 passing** (`python -m unittest discover -s tests`).

### 1.4 Authority walkthrough (recorded, no real email)
| Area | Coarse key | Authority id | Lifecycle | Result |
|---|---|---|---|---|
| Bengaluru | `12.97:77.59` | 3 | verified | cluster id 1 → **emailed**, `letters/letter_detection_22.pdf` |
| Kolkata | `22.71:88.42` | 4 | verified | cluster id 2 → **emailed**, `letters/letter_detection_15.pdf` |

Both demonstrated 3 distinct-IP matured reports → sweep → letter → routed to `codexzero98@gmail.com` via the app's own touchpoints (recording driver, nothing transmitted).

## 2. Work planned but NOT done yet

### 2.1 Corroboration threshold tuning in admin
`CORROBORATION_THRESHOLD` defaults 3; no admin surface yet to tune it live — planned (*not done*).

### 2.2 Large-image / threaded-server stress gate
Uploads proven non-500; a large BV/JPEG stress image end-to-end (and `client_ip` XFF-hop routing under a threaded server) is an open item.

### 2.3 Delivering emails to third-party authorities
Resend's `onramp@resend.dev` shared sender only delivers to the account owner. Emailing real authorities requires a **verified custom domain** in Resend (add a domain in the Resend dashboard, click "verify", add a `RESEND_FROM_EMAIL="SmartSurround <notices@yourdomain>"` override). *(DONE — subject to a custom-domain being added.)*

## 3. How to run & test (copy-paste, boolean-only output)

```powershell
cd "D:\Program\New folder\smartsurround_backend02\smartsurround_backend"
# 1) entire unit suite (email workflow + auth protection) — all must pass
python -m unittest discover -s tests -p "test_*.py" -v
# 2) live send (real email to EMAIL_TEST_RECIPIENT, requires valid RESEND_API_KEY in .env)
python app.py   # then: /admin -> login (ADMIN_PIN) -> "Send test notification email"
# 3) full corroboration walkthrough (3 distinct IPs same fine cell + matured window -> letter + email)
```

**Expected:** `test_*.py` → `OK` (25 tests), live test-email → `{'ok': True, 'mode': 'resend', 'to': 'codexzero98@gmail.com'}`.

## 4. Links
- `docs/LOGIN_PROTECTION.md` — auth/rate-limit/upload-token compliance matrix
- `docs/superpowers/specs/2026-09-16-corroboration-authority-email-design.md` — design spec
- `docs/superpowers/plans/2026-09-16-corroboration-authority-email.md` — implementation plan
- `docs/superpowers/plans/2026-09-16-trackb-email-wrap.md` — wrap/acceptance document
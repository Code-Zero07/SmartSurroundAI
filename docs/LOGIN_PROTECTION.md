# SmartSurround Login Protection — Compliance Matrix

> **Scope**: server-only (`app.py`), `templates/admin.html`, `templates/login.html`,
> `static/utils.js`. No frontend library, no CLAUDE.md, no legacy fallback paths.

---

## 1. Architecture overview

| Component | Transport | Lifetime | Visibility to JS | Purpose |
|-----------|-----------|----------|-----------------|---------|
| `ss_token` (httpOnly cookie) | httpOnly Set-Cookie | 30 min | **No** (browser-only) | Proves the user authenticated |
| Admin PIN (plain body) | POST form body / JSON `pin` field | per-request | Yes (user sees it) | Proves the user knows the secret |
| `ss_admin_pin` (localStorage) | client-side | until logout | Yes (user controls it) | Convenience auto-fill for locked actions |
| `ss_session_uid` (localStorage) | client-side | until logout | Yes | Random identity marker (audit trail) |
| Upload token | GET `/upload/token` → hidden form field or query param | 2 min, single-use | Yes (ephemeral) | Protects `/upload` without login |

---

## 2. Token + pin pairing (admin protected routes)

All admin mutation routes (`/admin/approve/<id>`, `/admin/reject/<id>`,
`/admin/authority/<id>/verify`, `/admin/authority/<id>/bounce`,
`/admin/authorities/add`, `/admin/test-email`) are behind the `@require_auth`
decorator, which requires:

1. A valid `ss_token` cookie (or `Authorization: Bearer <token>` /
   `X-Auth-Token` header, accepted for programmatic clients), **AND**
2. A correct pin in the form body / JSON body / query param.

Both must be valid for the action to proceed. Failure of either returns a
401 (no token) or 403 (wrong pin / rate-limited), with `{"ok": false,
"reason": "blocked"}` as the JSON body and a human-readable `"message"`.

---

## 3. Pin hashing: salted SHA-256

The bare pin is **never stored**. On boot `app.py` generates a 16-byte random
salt and derives:

```
ADMIN_DIGEST = salt_hex + ":" + sha256(salt_bytes || pin_utf8)
```

This digest lives in memory for the lifetime of the process.  Verification
re-derives the same value from the presented pin and compares using
`secrets.compare_digest` (constant-time).  The same scheme is implemented in
`static/utils.js` `build_verify_pin` / `verify_pin` so a client-side
pre-hash is possible if desired (the login page currently sends the bare pin
over the same-origin POST; both paths are equivalent for localhost HTTPS).

---

## 4. Rate limiting

| Key | Condition | Result |
|-----|-----------|--------|
| Per IP | 5 failed pin attempts within any window | 1-hour lockout (`rate_limited`, HTTP 429) |
| Token TTL | 30 minutes from issuance | Cookie expires, 401 on next action |
| Upload token | 2 minutes from issuance | Single-use, popped on first /upload POST |

---

## 5. CORS

`CORS_ORIGINS` is a comma-separated list of **exact origins** (no wildcards).
The `Access-Control-Allow-Origin` header is set to the matching origin when
present in the request `Origin`; otherwise no CORS headers are added.  The
default for local dev is `http://localhost:5000,http://127.0.0.1:5000`.

---

## 6. /upload tokenized flow

Citizens never log in.  Instead:

1. `/` loads and `static/app.js` calls `GET /upload/token`, receiving a short-
   lived, single-use token.
2. The token is injected as a hidden field `_upload_token` in the upload form.
3. `POST /upload` validates the token; without it the request is redirected to
   `/login` (hard block; the native HTML fallback is non-functional by design).

---

## 7. Login flow (admin)

```
GET  /login          → login page
POST /login/creds    → verify pin → issue token → Set-Cookie + X-Set-Auth-Token
GET  /admin          → cookie sends automatically, pin pre-filled from localStorage
POST /admin/approve  → @require_auth (cookie + pin in form body)
```

The pin is stored in `localStorage` as a plain-text convenience field (`ss_admin_pin`);
it never leaves the browser except as a per-request POST body field.  A random
`ss_session_uid` is also minted on first login for potential audit correlation.

---

## 8. "Blocked" message contract

Every 403 response from a protected route is a JSON body:

```json
{
  "ok": false,
  "reason": "blocked",
  "message": "Wrong pin. 3 attempts remaining."
}
```

The frontend displays the `message` field as a human-readable toast; the
`reason` field is always `"blocked"` for wrong-pin / rate-limit responses and
`"rate_limited"` for the 429 case.

---

## 9. What this is NOT

- **Not a production auth system**: single-process in-memory stores, no
  session revocation, no CSRF token, no refresh tokens.
- **Not a substitute for real IAM**: for a real deployment add bcrypt/argon2id
  KDFs, a persistent session store, CSRF protection, and 2FA.
- **No cloud dependency**: the pin gate is the only auth layer.  The email
  driver config (Resend / SMTP) is orthogonal.

---

## 10. File locations

| File | Role |
|------|------|
| `app.py` | All server-side auth logic: CORS, rate limiter, token store, pin digest, `@require_auth`, `/login/creds`, `/auth/token`, `/upload/token`, `/admin/api/clusters` |
| `templates/login.html` | PIN entry form, localStorage persistence |
| `templates/admin.html` | JS polling, toast notifications, locked-form gate |
| `static/utils.js` | `build_verify_pin`, `verify_pin`, UUID, entropy, breach check, latency |
| `.env` | `ADMIN_PIN` (plaintext, never committed), `CORS_ORIGINS` |
| `docs/LOGIN_PROTECTION.md` | This file |

---

*No CLAUDE.md reference.  No legacy fallback code paths.  No new pip
dependencies beyond those already in `requirements.txt` (Flask, resend,
python-dotenv, ultralytics).*

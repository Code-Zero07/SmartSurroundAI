# Upload Form — Camera Capture + GPS Auto-fill (Design)

Date: 2026-09-13

## Summary

Upgrade the citizen-facing test form (`templates/index.html`) so it can
(a) auto-fill the existing `lat` / `lon` fields from the browser's
Geolocation API and (b) take a photo directly with the camera instead of
only uploading an existing file. Both are pure progressive enhancements:
the back end (`app.py`), the `/upload` route, its field names, and the
`/admin` flow are untouched.

- Client logic lives in a new `static/app.js` (served automatically by
  Flask; no config change).
- `templates/index.html` gains only new markup; existing field
  names/action stay identical.
- `README.md` gains a short deployment note about the HTTPS ("secure
  context") requirement.

## Hard constraint — secure context

`navigator.geolocation` and `navigator.mediaDevices.getUserMedia` only
work in a secure context: HTTPS or `localhost`. Over plain
`http://<lan-ip>:5000` (how this is often tested from a phone) both
appear broken even though the code is correct.

- `app.js` checks `window.isSecureContext` first. If false, it reveals a
  banner — "Camera and location require HTTPS — serve this over
  localhost or a TLS URL (a LAN IP over plain http won't work)." — and
  does nothing else (form stays 100% native). It never silently fails or
  throws a confusing browser error.
- An in-file comment in `app.js` and a `README.md` note state that
  production (and phone testing from a LAN) needs a real TLS cert
  (Caddy/nginx + Let's Encrypt) or a tunnel (ngrok / Cloudflare Tunnel).

## Files touched

| File | Change |
|---|---|
| `templates/index.html` | Additive markup only; existing inputs/names/action unchanged |
| `static/app.js` | New — all client behavior |
| `README.md` | New "Camera & location" deployment note |

No backend files change. `detector.py`, `db.py`, `letter_generator.py`,
the model, the decision-layer logic, `templates/admin.html`, and `/admin`
are out of scope.

## Section 1 — Markup (`templates/index.html`)

All additions are additive and hidden by default; with no JS the DOM is
functionally today's form.

1. **Secure-context banner** — hidden `<div>` (`#secure-banner`),
   revealed only by JS when `!window.isSecureContext`. Never blocks the
   form.
2. **Location block** — the existing `lat` / `lon` text inputs stay
   exactly as-is (`name="lat"`, `name="lon"`, optional). Added around the
   row:
   - `#loc-explainer`: one line, "SmartSurround uses your location to tag
     this report accurately", shown only while a location request is
     pending.
   - `#loc-button`: "Use my location" (re-pin / retry).
   - `#loc-status`: status line, one of:
     - "Getting your location…"
     - "Location set (±Xm)" (from `position.coords.accuracy`)
     - "Couldn't get a fix — tap 'Use my location'"
     - "Location blocked — check your browser's site settings"
3. **Camera block** — `#camera-ui` (CSS-hidden until tier 1 succeeds)
   containing `<video>`, a **Capture** button, a **Retake** link, and a
   snapshot `<img>` slot. Default-visible buttons next to it:
   - `#take-photo`: "Take Photo" — the only trigger for `getUserMedia()`.
   - `#use-file`: "Use an existing photo instead" — opens the
     `#file-alternate` picker.
4. **Two file inputs, distinct roles:**
   - `#file-anchor`: `<input type="file" name="image" accept="image/*"
     required>` — the only input wired into the native form; always in
     the DOM, visible when no JS. This is tier 3 *and* the no-JS form.
   - `#file-alternate`: `<input type="file" accept="image/*"
     capture="environment">` (no `name`) — created by JS, used for
     "choose an existing file" and as the tier-2 picker when camera
     capture is unavailable/denied. Desktop browsers ignore `capture`
     and open a plain file dialog.
5. `source` select, `description` input, and submit button unchanged.

## Section 2 — Runtime behavior (`static/app.js`)

**`init()`** — one guarded entry, runs on `DOMContentLoaded`, wrapped in
try/catch: on *any* throw the script aborts silently and the native form
keeps today's behavior.

**Secure-context gate (first):** if `!window.isSecureContext`, reveal
`#secure-banner` and stop — no `noValidate`, no interception, no geo, no
camera. Form stays native.

**Successful `init()` steps (in order):**
1. `form.noValidate = true` — first thing after the secure-context gate.
   (Never set in the insecure / no-JS path, where the native `required`
   on `#file-anchor` keeps working.) Setting it first means a throw in
   the remaining wiring leaves validation off, so a native POST still
   works (the server's "missing image" redirect catches an empty
   `#file-anchor`) — rather than the worse state of a custom submit
   handler attached while native validation is still on, which would
   swallow the submit event entirely (the failure mode this prevents).
2. Wire up camera UI, location UI, and attach the submit handler.

**Camera flow (button-triggered only — never on page load):**
1. "Take Photo" click → if `navigator.mediaDevices?.getUserMedia` is
   missing, treat as tier-1 unavailable and go to step 5.
2. `getUserMedia({ video: { facingMode: { ideal: "environment" } },
   audio: false })`. Capture button disabled while pending. `ideal`
   (not `exact`) so laptops fall back to their one webcam and phones
   prefer the rear camera without failing.
3. Success → hide `#take-photo`, show `#camera-ui`. Set
   `video.muted = true; video.srcObject = stream; video.play()` —
   explicit mime/unmute before assign plus explicit `.play()`; the
   `autoplay` attribute alone is unreliable (Safari).
4. **Capture** → size a `<canvas>` to `video.videoWidth` ×
   `video.videoHeight` (never the element's CSS box) →
   `canvas.toBlob('image/jpeg', 0.92)` → store as `capturedBlob` →
   `stopStream()` → swap live video for a snapshot `<img>` from a blob
   URL → show **Retake** + "Use an existing photo instead".
   - If `toBlob` resolves `null` (tainted canvas): treat as capture
     failure — inline "Capture failed — try again" and surface Retake;
     never submit.
5. Failure/denied (NotAllowedError / NotFoundError / etc.) → `stopStream()`
   → one-line note "Camera unavailable — picking a file instead" → try
   `#file-alternate.click()`. **iOS Safari may block the auto-open**
   (user activation can expire across the Promise); if the picker
   doesn't open, reveal `#file-alternate` inline with a visible "Choose a
   photo" button.
6. **Retake** calls the *same* `startCamera()` acquisition function as
   the first attempt — same success path, same failure path. It clears
   `capturedBlob` (and the snapshot object URL) first.
7. **"Use an existing photo instead"** (visible both before and after
   capture) → `clearCapture()` (stop stream, clear `capturedBlob`, revoke
   snapshot URL) → open `#file-alternate` picker.

**Stream hygiene:** a single `stopStream()` helper — stops every track on
the active stream and clears its ref. Called after Capture, before the
file route, on Retake's failure path, and before the fetch fires.

**Geolocation (auto on load, secure context only):**
1. If `navigator.geolocation` exists (existence check first — the status
   line must never get stuck on "Getting your location…"): show
   `#loc-explainer` + status "Getting your location…", then
   `getCurrentPosition(success, err, { enableHighAccuracy: true,
   timeout: 10000, maximumAge: 30000 })`.
2. Success → fill `lat` / `lon` (~6 decimals), status "Location set
   (±Xm)", hide explainer. **Never clobber user input:** each field gets
   an 'input' listener that sets an "edited by user" flag for that
   field; the success callback fills only fields the user has not
   edited (applies both to the auto-request and the "Use my location"
   button). A slow GPS fix while the citizen types their own
   coordinates must not silently overwrite them.
3. `PERMISSION_DENIED` → status "Location blocked — check your browser's
   site settings". The "Use my location" button detects prior hard denial
   and shows the same guidance instead of re-calling (a denied API does
   not re-trigger the native prompt anyway).
4. `TIMEOUT` / `POSITION_UNAVAILABLE` → "Couldn't get a fix — tap 'Use
   my location'". The button retries.
5. Success/failure never touches the editable `lat`/`lon` inputs beyond
   filling them; a failure leaves them empty and editable. Location is
   never required for submission.

**Submit (JS active + secure context only):**
1. `preventDefault()`. Image-source priority, first existing wins:
   1. `capturedBlob` (tier-1 capture)
   2. `#file-alternate.files[0]`
   3. `#file-anchor.files[0]`
   If none → inline "Please take or choose an image first." and return —
   no POST.
   - **Stale-capture rule:** choosing a file via `#file-alternate` after
     a prior capture clears `capturedBlob`, so the newly chosen file
     wins; the priority above can never submit an old capture over an
     explicit new choice.
2. Guard: `lat` / `lon` if non-empty must be finite numbers; else inline
   "Latitude/Longitude must be numbers (or leave blank)" and return —
   this is the most common server 500 (`float()` in `/upload`).
3. Build `FormData`: `source`, `lat`, `lon`, `description` read from the
   form controls; `image` appended manually per the priority (blob as
   `capture.jpg`, else the chosen File). Disable submit button
   (label "Uploading…") — disabled, not just relabeled, so a fast
   double-click can't double-POST.
4. `fetch('/upload', { method: 'POST', body: fd, redirect: 'manual' })`.
   - Resolve + `resp.type === "opaqueredirect"` → `location.href = "/"`.
     `redirect: 'manual'` stops fetch from auto-following the redirect
     and consuming the Flask flash internally, so the navigate renders
     the server's flash message normally.
   - Resolve + `resp.status >= 400` → inline "The server hit an error —
     please try again." and re-enable the button. (A bare 500 is not a
     redirect, so its status *is* readable with `redirect: 'manual'`.)
   - Network reject → inline "Submission failed — check your connection
     and try again." and re-enable the button.
   - `stopStream()` before the fetch fires.

## Section 3 — Error handling & fallback matrix

Every listed outcome preserves the invariant: the form still submits.

| Condition | Result |
|---|---|
| JS absent / throws in `init()` | Native form; `#file-anchor` (tier 3) works as today |
| Insecure context | `#secure-banner` shown; form fully native |
| `navigator.geolocation` missing | Silent skip; manual inputs only |
| location denied | "Location blocked…" guidance; lat/lon empty + editable |
| location timeout/unavailable | "Couldn't get a fix…"; button retries |
| `mediaDevices` missing | "Take Photo" → straight to file picker |
| camera denied / not found | Note + `#file-alternate` picker (inline reveal fallback on iOS) |
| capture succeeds | `capturedBlob` stored; stream stopped; snapshot + Retake |
| `toBlob` → null | "Capture failed — try again"; Retake; nothing submitted |
| capture then pick file via `#file-alternate` | `capturedBlob` cleared; chosen file wins |
| no image at submit | Inline "Please take or choose an image first."; no POST |
| bad non-numeric lat/lon | Inline "Latitude/Longitude must be numbers…"; no POST |
| fetch → redirect | Navigate to `/`; server flash renders normally |
| fetch → 500 | Inline "The server hit an error…"; button re-enabled |
| fetch → network reject | Inline connection message; button re-enabled |

Naming guarantees: `image` appended manually in every JS path;
`source`/`lat`/`lon`/`description` read from the live form controls,
exactly as they'd have been serialized natively.

## Section 4 — Verification, README, regression

No automated test framework exists in the repo; verification is the
manual matrix below.

| # | Criterion | Expectation |
|---|---|---|
| 1 | Laptop with webcam | "Take Photo" → preview → Capture → usable photo → form submits to `/upload` |
| 2 | Phone (Android + iOS) | Rear camera default; capture works; form submits |
| 3 | Deny camera | Fall back to file upload; page intact |
| 4 | Deny location | lat/lon editable + empty; submission never blocked |
| 5 | Plain HTTP from non-localhost | `#secure-banner` explains HTTPS; no silent failure |
| 6 | No JS / no `getUserMedia` | Plain file-upload form identical to today |
| 7 | `/admin` + letter PDF regression | Queue, approve, letter generation unchanged |
| 8 | Capture → switch to file → pick a different file | Submit and confirm the newly chosen file (not the earlier `capturedBlob`) is what actually reaches `/upload` — the silent-wrong-photo case requires an explicit test line |

Also test explicitly on iOS Safari: Take Photo → deny → confirm the file
picker opens, or the inline "Choose a photo" button appears (auto-open
may be blocked after the Promise).

**README note (content):** camera and location both require a secure
context; `localhost` is exempt; to test from a phone over a LAN or serve
production, use TLS (Caddy/nginx + Let's Encrypt) or a tunnel
(ngrok/Cloudflare Tunnel); over plain `http://<lan-ip>:5000` both will
appear broken even though the code is correct.

**Regression:** `app.py`, `/admin`, and `templates/admin.html` are not
modified; verify the queue/letter path once after the change.

## Non-goals

- No changes to `detector.py`, `db.py`, `letter_generator.py`, the
  model, or decision/acceptance thresholds.
- No changes to `/upload` (field names/behavior) or the `/admin` flow.
- No backend error-handling changes; server 500s are surfaced by the
  client via the `redirect: 'manual'` status check.
- No automated tests (none exist in the repo); verification is manual.
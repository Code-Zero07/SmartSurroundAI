# Upload Form — Camera Capture + GPS Auto-fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the citizen test form's plain file picker into progressive camera capture and auto-fill its lat/lon from the browser's Geolocation API, without any backend change.

**Architecture:** All client logic lives in one new `static/app.js` (an IIFE on `DOMContentLoaded`) that gradually enhances the existing form: secure-context gate → geolocation → camera tiers → fetch-based submit. On any JS failure the native form (today's behavior) is untouched. Zero backend changes.

**Tech Stack:** Flask (serving only — untouched), vanilla ES5-ish JavaScript (no build step, no libraries), HTML5 `getUserMedia` / `canvas.toBlob` / Geolocation API.

**Spec:** `docs/superpowers/specs/2026-09-13-upload-form-camera-geolocation-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- **Backend frozen:** do NOT modify `app.py`, `db.py`, `detector.py`, `letter_generator.py`, the model, or `templates/admin.html`. Do NOT change the `/upload` route, its field names (`image`, `source`, `lat`, `lon`, `description`), or the form's `action`/`enctype`.
- **No git commits:** this folder is not a git repo and the user asked for no commits. There are no commit steps in this plan.
- **Progressive enhancement:** with JS absent or failing, the DOM must be functionally today's form. Native `required` on `#file-anchor` must keep working in the no-JS path.
- **Secure context:** both geo and camera are only attempted when `window.isSecureContext` is true. Otherwise show `#secure-banner` and leave the form native.
- **Never block submission:** no JS path may make the form unusable; on insecure context, denial, or missing APIs the file-upload path still works.
- **Manual verification only:** the repo has no test harness. Every task ends with concrete manual checks; Task 6 runs the full acceptance matrix.
- **Touched files (all of them):** `templates/index.html`, `static/app.js` (new), `README.md`. Nothing else.

---

### Task 1: Static markup in `templates/index.html`

**Files:**
- Modify: `templates/index.html` (rewrite: keep existing head/styles, form fields, names, and the admin link; add markup + CSS below)

**Interfaces:**
- Consumes: none.
- Produces: elements the later `app.js` tasks rely on, by id: `#upload-form`, `#secure-banner`, `#file-anchor`, `#lat`, `#lon`, `#loc-explainer`, `#loc-button`, `#loc-status`, `#photo-picker`, `#take-photo`, `#use-file`, `#camera-ui`, `#camera-preview`, `#capture-canvas`, `#snapshot-img`, `#capture-btn`, `#retake-btn`, `#form-error`, `#submit-btn`. Element ids must match exactly.

- [ ] **Step 1: Write the new `templates/index.html`**

Replace the whole file with:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SmartSurround — Road Damage Detection (Test Console)</title>
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 640px;
           margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
    h1 { font-size: 1.4rem; }
    .note { background: #eef7f0; border-left: 3px solid #2f9e44; padding: 10px 14px;
            font-size: 0.9rem; margin-bottom: 24px; }
    form { display: flex; flex-direction: column; gap: 12px; }
    label { font-size: 0.85rem; font-weight: 600; }
    input[type=text], input[type=file] { padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
    .row { display: flex; gap: 12px; }
    .row > div { flex: 1; }
    button { padding: 10px 16px; border: none; border-radius: 6px; background: #2f9e44;
             color: white; font-weight: 600; cursor: pointer; }
    button[type=button] { background: #3b5bdb; }
    .flash { background: #fff3bf; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; }
    a.admin-link { display: inline-block; margin-top: 24px; }
    #secure-banner { background: #fff3bf; border-left: 3px solid #e67700; padding: 10px 14px;
                     font-size: 0.9rem; margin-bottom: 16px; }
    #loc-status { font-size: 0.85rem; color: #555; margin-left: 8px; }
    #camera-ui video, #camera-ui img { max-width: 100%; height: auto; background: #000;
                                       border-radius: 6px; display: block; margin-bottom: 8px; }
    #form-error { background: #f8d7da; border-left: 3px solid #c92a2a; padding: 10px 14px;
                  font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>SmartSurround — Detection Test Console</h1>
  <div class="note">
    This form stands in for the ESP32-CAM firmware and the citizen-complaint
    portal (both will POST to the same <code>/upload</code> endpoint). Upload
    any road photo to push it through detection &rarr; the admin verification queue.
  </div>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endif %}
  {% endwith %}

  <div id="secure-banner" hidden>
    Camera and location require HTTPS — serve this over localhost or a TLS URL
    (a LAN IP over plain http won't work).
  </div>

  <form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
    <div>
      <label>Image</label><br>
      <input type="file" id="file-anchor" name="image" accept="image/*" required>
    </div>

    <div id="photo-picker">
      <button type="button" id="take-photo" hidden>Take Photo</button>
      <button type="button" id="use-file" hidden>Use an existing photo instead</button>
    </div>

    <div id="camera-ui" hidden>
      <video id="camera-preview" playsinline autoplay muted></video>
      <canvas id="capture-canvas" hidden></canvas>
      <img id="snapshot-img" alt="Captured photo" hidden>
      <div>
        <button type="button" id="capture-btn" disabled>Capture</button>
        <button type="button" id="retake-btn" hidden>Retake</button>
      </div>
    </div>

    <div>
      <label>Source</label><br>
      <select name="source">
        <option value="citizen">Citizen submission</option>
        <option value="esp32">ESP32-CAM post</option>
      </select>
    </div>
    <div class="row">
      <div>
        <label>Latitude (optional)</label><br>
        <input type="text" id="lat" name="lat" placeholder="22.5726">
      </div>
      <div>
        <label>Longitude (optional)</label><br>
        <input type="text" id="lon" name="lon" placeholder="88.3639">
      </div>
    </div>
    <p id="loc-explainer" hidden>SmartSurround uses your location to tag this report accurately.</p>
    <div>
      <button type="button" id="loc-button" hidden>Use my location</button>
      <span id="loc-status"></span>
    </div>
    <div>
      <label>Description (optional, citizen note)</label><br>
      <input type="text" name="description" placeholder="Large pothole near the bus stop">
    </div>
    <button type="submit" id="submit-btn">Run Detection</button>
  </form>

  <p id="form-error" hidden></p>

  <a class="admin-link" href="/admin">Go to admin verification queue &rarr;</a>
  <script src="/static/app.js" defer></script>
</body>
</html>
```

- [ ] **Step 2: Manual check — no-JS baseline unchanged**

Run `python app.py`, open `http://localhost:5000`, disable JS (or use a webview without JS). Confirm: the page is today's form — one "Image" file input (name=`image`, required), source select, lat/lon text fields, description, "Run Detection". All new UI (`#take-photo`, `#use-file`, banner, etc.) is hidden by the `hidden` attribute; `#form-error` is invisible; `/static/app.js` returns 404 (not yet created).

---

### Task 2: `static/app.js` skeleton — secure gate, noValidate, init guard

**Files:**
- Create: `static/app.js`
- Modify: none

**Interfaces:**
- Consumes: element ids from Task 1.
- Produces: module-scoped refs (`form`, `video`, `captureBtn`, ...), mutable state (`activeStream`, `capturedBlob`, `snapshotUrl`, `userEditedLat`, `userEditedLon`, `locBlocked`), helpers `stopStream()`, `resetCaptureState()`, `clearCapture()`. Later tasks depend on these exact names.

- [ ] **Step 1: Create `static/app.js` with the skeleton + secure gate + init**

Write the file with the shared refs, state, `stopStream`/`resetCaptureState`/`clearCapture` helpers (leaf utilities — Task 4 exercises them), the insecure-context gate, `form.noValidate = true` first, wiring calls, and `DOMContentLoaded` guard. Placeholders marked `// Task 3` / `// Task 4` / `// Task 5` are filled by later tasks — do NOT remove them.

```js
/* SmartSurround test-form enhancements: camera capture + GPS auto-fill.
 *
 * SECURE CONTEXT: navigator.geolocation and navigator.mediaDevices.getUserMedia
 * both require HTTPS (or localhost). Over plain http://<lan-ip>:5000 they will
 * appear broken even though this code is correct — production (or phone testing
 * over a LAN) needs TLS (Caddy/nginx + Let's Encrypt) or a tunnel
 * (ngrok / Cloudflare Tunnel). The secure-banner handles the detection.
 *
 * This is a progressive enhancement: if any of init() throws, the native form
 * (today's markup) is left completely untouched and still works.
 */
(function () {
  "use strict";

  var form = document.getElementById("upload-form");
  if (!form) return;

  var video = document.getElementById("camera-preview");
  var captureBtn = document.getElementById("capture-btn");
  var retakeBtn = document.getElementById("retake-btn");
  var snapshotImg = document.getElementById("snapshot-img");
  var canvas = document.getElementById("capture-canvas");
  var cameraUi = document.getElementById("camera-ui");
  var takePhotoBtn = document.getElementById("take-photo");
  var useFileBtn = document.getElementById("use-file");
  var fileAnchor = document.getElementById("file-anchor");
  var secureBanner = document.getElementById("secure-banner");
  var locExplainer = document.getElementById("loc-explainer");
  var locStatus = document.getElementById("loc-status");
  var locButton = document.getElementById("loc-button");
  var latInput = document.getElementById("lat");
  var lonInput = document.getElementById("lon");
  var errorLine = document.getElementById("form-error");
  var submitBtn = document.getElementById("submit-btn");

  var activeStream = null;
  var capturedBlob = null;
  var snapshotUrl = null;
  var userEditedLat = false;
  var userEditedLon = false;
  var locBlocked = false;

  function stopStream() {
    if (activeStream) {
      activeStream.getTracks().forEach(function (t) { t.stop(); });
      activeStream = null;
    }
    if (video) {
      video.srcObject = null;
      video.hidden = false;
    }
  }

  function resetCaptureState() {
    capturedBlob = null;
    if (snapshotUrl) { URL.revokeObjectURL(snapshotUrl); snapshotUrl = null; }
    snapshotImg.hidden = true;
    snapshotImg.removeAttribute("src");
    video.hidden = false;
    retakeBtn.hidden = true;
    captureBtn.disabled = true;
  }

  function clearCapture() {
    stopStream();
    resetCaptureState();
  }

  function init() {
    if (!window.isSecureContext) {
      secureBanner.hidden = false;
      return;
    }

    form.noValidate = true;

    wireLocation(); // Task 3
    wireCamera();   // Task 4
    form.addEventListener("submit", onSubmit); // Task 5

    // Swap native controls for enhanced ones LAST, so a throw anywhere above
    // leaves the native form fully intact.
    takePhotoBtn.hidden = false;
    useFileBtn.hidden = false;
    fileAnchor.hidden = true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    try {
      init();
    } catch (err) {
      // Leave the native form untouched.
    }
  });
})();
```

- [ ] **Step 2: Manual check — route works, page unchanged**

Create `static/` first (Flask serves it automatically once the file exists). Reopen `http://localhost:5000` with JS enabled: the page must look identical to Task 1's no-JS state (all enhanced UI still hidden, anchor visible), the console must be clean, and `/static/app.js` must load (Network tab, 200). Environment note: `localhost` is a secure context, so the banner stays hidden and `init()` reaches the wiring calls (which do nothing yet since Tasks 3–5 aren't present — the `// Task N` lines are literal comments until filled).

---

### Task 3: Geolocation auto-fill + "Use my location"

**Files:**
- Modify: `static/app.js` — replace `wireLocation(); // Task 3` with the wiring call bodies below (and add the functions to the IIFE scope).

**Interfaces:**
- Consumes: `latInput`, `lonInput`, `locExplainer`, `locStatus`, `locButton`, and state flags from Task 2.
- Produces: `wireLocation()` (called from `init()`), `requestLocation()`. Sets `userEditedLat`/`userEditedLon` on input; sets `locBlocked` on hard denial.

- [ ] **Step 1: Replace the `// Task 3` placeholder with the location implementation**

Replace the line `wireLocation(); // Task 3` with `wireLocation();` and append to the file (inside the IIFE, before the closing `})();`):

```js
  function wireLocation() {
    if (!navigator.geolocation) return; // API missing: silent skip, never a stuck status line

    locButton.hidden = false;

    locExplainer.hidden = false;
    locStatus.textContent = "Getting your location…";
    requestLocation();

    latInput.addEventListener("input", function () { userEditedLat = true; });
    lonInput.addEventListener("input", function () { userEditedLon = true; });

    locButton.addEventListener("click", function () {
      if (locBlocked) {
        // A denied permission does not re-trigger the native prompt; don't imply it will.
        locStatus.textContent = "Location blocked — check your browser's site settings";
        return;
      }
      locExplainer.hidden = false;
      locStatus.textContent = "Getting your location…";
      requestLocation();
    });
  }

  function requestLocation() {
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        locExplainer.hidden = true;
        locStatus.textContent = "Location set (±" + Math.round(pos.coords.accuracy) + "m)";
        // Never clobber what the citizen typed while GPS was still resolving.
        if (!userEditedLat) latInput.value = pos.coords.latitude.toFixed(6);
        if (!userEditedLon) lonInput.value = pos.coords.longitude.toFixed(6);
      },
      function (err) {
        locExplainer.hidden = true;
        if (err.code === err.PERMISSION_DENIED) {
          locBlocked = true;
          locStatus.textContent = "Location blocked — check your browser's site settings";
        } else {
          locStatus.textContent = "Couldn't get a fix — tap 'Use my location'";
        }
        // lat/lon stay empty and editable in every failure case.
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
    );
  }
```

- [ ] **Step 2: Manual check — geo happy path and no-clobber**

With JS enabled on `http://localhost:5000`: allow the location prompt → "Location set (±Xm)" appears, `lat`/`lon` fill with ~6-decimal values, explainer hides. Reload; **type** a latitude by hand before the fix lands (wait for it to resolve) → the typed value must NOT be replaced. Click "Use my location" → a fresh fix overwrites (only because you didn't edit). Deny location on a reload → "Location blocked — check your browser's site settings", fields empty and editable, form still submits.

---

### Task 4: Camera capture (tier 1) + file-picker fallbacks (tiers 2/3)

**Files:**
- Modify: `static/app.js` — replace `wireCamera();   // Task 4` with `wireCamera();` and add the functions below.

**Interfaces:**
- Consumes: `video`, `captureBtn`, `retakeBtn`, `snapshotImg`, `canvas`, `cameraUi`, `takePhotoBtn`, `useFileBtn`, `errorLine`, `activeStream`, `capturedBlob`, `snapshotUrl`, and `stopStream`/`resetCaptureState`/`clearCapture` from Task 2.
- Produces: `wireCamera()`, `startCamera()` (reused by retake — same success AND failure path), `openFilePicker()`, and a module-scoped `fileAlt` input. Establishes the stale-capture rule: file selection clears `capturedBlob`. `capturedBlob` (Blob, jpeg) is consumed by Task 5.

- [ ] **Step 1: Replace the `// Task 4` placeholder and add the camera logic**

Replace `wireCamera();   // Task 4` with `wireCamera();` and append:

```js
  var fileAlt = null;

  function ensureFileAlt() {
    if (!fileAlt) {
      fileAlt = document.createElement("input");
      fileAlt.type = "file";
      fileAlt.accept = "image/*";
      fileAlt.capture = "environment"; // mobile: straight to the native camera
      fileAlt.addEventListener("change", function () {
        if (fileAlt.files && fileAlt.files[0]) {
          // Stale-capture rule: an explicitly chosen file wins over any earlier capture.
          clearCapture();
        }
      });
      if (useFileBtn) useFileBtn.after(fileAlt);
    }
    return fileAlt;
  }

  function wireCamera() {
    takePhotoBtn.addEventListener("click", function () {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        openFilePicker("Camera not supported here — pick a file instead.");
        return;
      }
      startCamera();
    });

    useFileBtn.addEventListener("click", function () {
      clearCapture();
      openFilePicker();
    });

    captureBtn.addEventListener("click", onCapture);

    retakeBtn.addEventListener("click", function () {
      clearCapture();
      // Same acquisition function as the first attempt: success restarts the live
      // view, failure/denial falls through to openFilePicker like the first time.
      startCamera();
    });
  }

  function startCamera() {
    captureBtn.disabled = true;
    stopStream();

    // ideal (not exact): rear camera preferred on phones, lone webcam on laptops,
    // never fatal if the requested facing isn't available.
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (stream) {
        activeStream = stream;
        video.muted = true;          // Safari needs this set with srcObject assignment
        video.srcObject = stream;
        return video.play();         // explicit play(), autoplay attr is unreliable
      })
      .then(function () {
        cameraUi.hidden = false;
        takePhotoBtn.hidden = true;
        snapshotImg.hidden = true;
        captureBtn.disabled = false;
      })
      .catch(function () {
        clearCapture();
        openFilePicker("Camera unavailable — picking a file instead.");
      });
  }

  function onCapture() {
    var w = video.videoWidth;
    var h = video.videoHeight;
    if (!w || !h) return; // stream not ready yet
    canvas.width = w;     // stream resolution, NOT the element's CSS size
    canvas.height = h;
    canvas.getContext("2d").drawImage(video, 0, 0, w, h);
    canvas.toBlob(function (blob) {
      if (!blob) { // tainted canvas / encoder failure
        clearCapture();
        errorLine.textContent = "Capture failed — try again.";
        errorLine.hidden = false;
        startCamera();
        return;
      }
      capturedBlob = blob;
      stopStream(); // webcam indicator off once a frame is captured
      snapshotUrl = URL.createObjectURL(blob);
      snapshotImg.src = snapshotUrl;
      snapshotImg.hidden = false;
      video.hidden = true;
      captureBtn.disabled = true;
      retakeBtn.hidden = false;
    }, "image/jpeg", 0.92);
  }

  function openFilePicker(message) {
    if (message) {
      errorLine.textContent = message;
      errorLine.hidden = false;
    }
    ensureFileAlt();
    fileAlt.click();
    // iOS Safari may swallow programmatic .click() here (user activation expired
    // across the Promise). Reveal the input inline so the citizen always has a
    // visible way to choose a photo; harmless if the picker also opened.
    fileAlt.hidden = false;
    fileAlt.style.maxWidth = "100%";
  }
```

- [ ] **Step 2: Manual check — laptop tier-1 flow**

Laptop with webcam, `https://localhost`-equivalent (use `http://localhost:5000`): "Take Photo" → preview appears; Capture → video swaps to snapshot, Capture disables, Retake appears; webcam indicator light turns off. Retake → live preview resumes. "Use an existing photo instead" → file picker opens; pick a file → snapshot cleared. Deny camera permission on a fresh load ("Take Photo" → deny) → note text + file picker/`#file-alt` appears, page intact.

---

### Task 5: Fetch-based submit with image priority + error surfacing

**Files:**
- Modify: `static/app.js` — replace `form.addEventListener("submit", onSubmit); // Task 5` with the real call and add `onSubmit` + `getImageSource`.

**Interfaces:**
- Consumes: `form`, `submitBtn`, `errorLine`, `latInput`, `lonInput`, `capturedBlob`, `fileAlt`, `fileAnchor`, `stopStream`.
- Produces: `onSubmit(event)` — attached to the form `submit` event. Nothing else consumes output; this is the terminal wiring.

- [ ] **Step 1: Replace the `// Task 5` placeholder and add the submit handler**

Replace `form.addEventListener("submit", onSubmit); // Task 5` with `form.addEventListener("submit", onSubmit);` and append:

```js
  function getImageSource() {
    if (capturedBlob) return capturedBlob;
    if (fileAlt && fileAlt.files && fileAlt.files[0]) return fileAlt.files[0];
    if (fileAnchor && fileAnchor.files && fileAnchor.files[0]) return fileAnchor.files[0];
    return null;
  }

  function onSubmit(event) {
    event.preventDefault();

    var imageFile = getImageSource();
    if (!imageFile) {
      errorLine.textContent = "Please take or choose an image first.";
      errorLine.hidden = false;
      return;
    }

    var latVal = latInput.value.trim();
    var lonVal = lonInput.value.trim();
    if ((latVal !== "" && !isFinite(parseFloat(latVal))) ||
        (lonVal !== "" && !isFinite(parseFloat(lonVal)))) {
      errorLine.textContent = "Latitude/Longitude must be numbers (or leave blank).";
      errorLine.hidden = false;
      return;
    }

    stopStream();

    var fd = new FormData(form); // source, lat, lon, description serialize as natively
    fd.delete("image");          // image is appended manually, with the right priority
    if (capturedBlob) {
      fd.append("image", capturedBlob, "capture.jpg");
    } else {
      fd.append("image", imageFile); // picked/uploaded file keeps its own name
    }

    submitBtn.disabled = true; // disabled, not just relabeled: no double-POST
    var originalLabel = submitBtn.textContent;
    submitBtn.textContent = "Uploading…";

    fetch("/upload", { method: "POST", body: fd, redirect: "manual" })
      .then(function (resp) {
        if (resp.type === "opaqueredirect") {
          // redirect: 'manual' stops fetch from auto-following the 302 and consuming
          // the Flask flash internally; navigating renders it normally.
          window.location.href = "/";
        } else if (resp.status >= 400) {
          // A bare error response is NOT a redirect, so its status is readable.
          submitBtn.disabled = false;
          submitBtn.textContent = originalLabel;
          errorLine.textContent = "The server hit an error — please try again.";
          errorLine.hidden = false;
        } else {
          window.location.href = "/";
        }
      })
      .catch(function () {
        submitBtn.disabled = false;
        submitBtn.textContent = originalLabel;
        errorLine.textContent = "Submission failed — check your connection and try again.";
        errorLine.hidden = false;
      });
  }
```

- [ ] **Step 2: Manual check — submit paths**

Laptop, JS enabled: run through each — (a) Upload a file via "Use an existing photo instead" → "Run Detection" → server flash "…queued for admin verification" on `/`; (b) no image at all → inline "Please take or choose an image first." and no request sent; (c) type `abc` into Latitude → inline numbers error, no request; (d) capture a photo then submit → flash as in (a); (e) double-click "Run Detection" fast → only one request (Network tab).

---

### Task 6: README note + full acceptance matrix

**Files:**
- Modify: `README.md` (append a short section)

- [ ] **Step 1: Append the deployment note to `README.md`**

After the "Then open **http://localhost:5000**." paragraph, add:

```markdown
## Camera & location in the test form

The upload form's camera capture and GPS auto-fill use browser APIs that
only work in a **secure context** (HTTPS, or `localhost`). Served over a
plain `http://<lan-ip>:5000`, both will appear broken even though the
code is correct — the page shows a notice explaining this. For phone
testing from a LAN or production, serve over TLS (Caddy/nginx + Let's
Encrypt) or a tunnel (ngrok / Cloudflare Tunnel). Nothing here changes
the `/upload` endpoint or the admin flow.
```

- [ ] **Step 2: Run the full manual acceptance matrix**

Start `python app.py`; with JS enabled and a webcam, on a laptop plus a phone (Android and iOS if available), run the spec's Section 4 table:

1. Laptop webcam: "Take Photo" → preview → Capture → usable photo → submits to `/upload`.
2. Phone: rear camera default (ideal facingMode), capture works, submits.
3. Deny camera: falls back to file upload; page intact.
4. Deny location: lat/lon editable + empty; submission never blocked.
5. Plain HTTP from non-localhost (`http://<lan-ip>:5000`): `#secure-banner` explains HTTPS; no silent failure.
6. No JS / no `getUserMedia`: plain file-upload form identical to today.
7. `/admin` queue + approve + letter PDF: unchanged and working.
8. **Stale-capture:** capture a photo, then "Use an existing photo instead" → pick a *different* file → submit → confirm the newly chosen file (not the earlier capture) is what reaches `/upload` (check the stored image under `/admin`).
9. iOS Safari specifically: "Take Photo" → **deny** → confirm the file picker opens OR the inline choose-file input appears (auto-open may be blocked after the Promise).
10. Regression: submit with garbage coords blocked client-side; force a server error (pick a non-image file) → inline "The server hit an error…" rather than a blank navigate.
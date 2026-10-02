/* SmartSurround citizen wizard (5 steps).
 *
 * Every API call needs a FRESH single-use upload token (they are consumed).
 * The backend is authoritative for detection/coords/routing:
 *   - location_name is editable, authority_area is NOT (server-derived).
 *   - preview re-runs the model; submit finalizes the stored draft only.
 * No email is sent anywhere on the citizen path.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var nav = Array.prototype.slice.call(document.querySelectorAll("#step-nav button"));
  var steps = [1, 2, 3, 4, 5].map(function (n) { return $("step-" + n); });

  var state = {
    lat: null, lon: null, locationName: "", authorityArea: null,
    imageFile: null, detection: null, draftId: null
  };

  function token() {
    return fetch("/upload/token")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return d && d.ok ? d.token : ""; });
  }

  function showStep(n) {
    steps.forEach(function (s, i) { s.classList.toggle("active", i + 1 === n); });
    nav.forEach(function (b, i) { b.classList.toggle("active-step", i + 1 === n); });
    window.scrollTo(0, 0);
  }

  function error(msg) {
    var el = $("form-error");
    el.textContent = msg;
    el.hidden = false;
  }
  function clearError() { $("form-error").hidden = true; }
  function detectError(msg) {
    var el = $("detect-error");
    el.textContent = msg;
    el.hidden = !msg;
  }
  function setDetectStatus(msg) {
    var el = $("photo-detect-status");
    if (!el) return;
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  // ---- Location -----------------------------------------------------------
  var marker = null, map = null;
  function putLatLon(lat, lon) {
    state.lat = lat; state.lon = lon;
    $("lat").value = lat.toFixed(6);
    $("lon").value = lon.toFixed(6);
    if (map && marker) marker.setLatLng([lat, lon]);
    else if (map) marker = L.marker([lat, lon], { draggable: true }).addTo(map);
    if (map) map.setView([lat, lon], 16);
    geocode(lat, lon);
    $("loc-next").disabled = false;
  }

  function geocode(lat, lon) {
    token().then(function (t) {
      if (!t) return;
      return fetch("/api/geocode?lat=" + lat + "&lon=" + lon + "&_upload_token=" + t)
        .then(function (r) { return r.json(); });
    }).then(function (d) {
      if (!d) return;
      state.authorityArea = d.authority_area || null;
      $("authority_area").textContent = d.authority_area || "Area " +
        Math.round(lat * 100) / 100 + ":" + Math.round(lon * 100) / 100;
      if (d.location_name) $("location_name").value = d.location_name;
    });
  }

  function initMap() {
    if (typeof L === "undefined") return;
    map = L.map("map").setView([22.5726, 88.3639], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© OpenStreetMap"
    }).addTo(map);
    map.on("click", function (e) { putLatLon(e.latlng.lat, e.latlng.lng); });
  }

  function requestGeo() {
    if (!navigator.geolocation) { $("loc-status").textContent = "Geolocation unavailable"; return; }
    $("loc-status").textContent = "Getting your location…";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        $("loc-status").textContent = "Location set (±" + Math.round(pos.coords.accuracy) + "m)";
        putLatLon(pos.coords.latitude, pos.coords.longitude);
      },
      function (err) {
        $("loc-status").textContent = err.code === err.PERMISSION_DENIED
          ? "Location blocked — check browser site settings" : "Couldn't get a fix — tap again";
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 });
  }

  // ---- Photo (ported from the old app.js flow) -----------------------------
  var video = $("camera-preview"), canvas = $("capture-canvas"),
      snapshotImg = $("snapshot-img");
  var activeStream = null, capturedBlob = null, fileAlt = null;

  function stopStream() {
    if (activeStream) { activeStream.getTracks().forEach(function (t) { t.stop(); }); activeStream = null; }
    if (video) video.srcObject = null;
  }
  function startCamera() {
    stopStream();
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (stream) {
        activeStream = stream; video.muted = true; video.srcObject = stream; return video.play();
      })
      .then(function () {
        $("camera-ui").hidden = false; $("take-photo").hidden = true;
        $("capture-btn").disabled = false; $("snapshot-img").hidden = true;
      })
      .catch(function () { openFilePicker("Camera unavailable — selecting a file instead."); });
  }
  function onCapture() {
    var w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(video, 0, 0, w, h);
    canvas.toBlob(function (blob) {
      if (!blob) { clearError(); error("Capture failed — retake."); return; }
      capturedBlob = blob;
      state.imageFile = capturedBlob;
      stopStream();
      snapshotImg.src = URL.createObjectURL(blob);
      snapshotImg.hidden = false; video.hidden = true;
      $("capture-btn").disabled = true; $("retake-btn").hidden = false;
      $("photo-next").disabled = false;
      startDetection();
    }, "image/jpeg", 0.92);
  }
  function ensureFileAlt() {
    if (fileAlt) return fileAlt;
    fileAlt = document.createElement("input");
    fileAlt.type = "file"; fileAlt.accept = "image/*";
    fileAlt.addEventListener("change", function () {
      if (fileAlt.files && fileAlt.files[0]) {
        state.imageFile = fileAlt.files[0];
        capturedBlob = null;
        $("photo-next").disabled = false;
        snapshotImg.hidden = true;
        startDetection();
      }
    });
    document.body.appendChild(fileAlt);
    return fileAlt;
  }
  function openFilePicker(message) {
    if (message) { clearError(); error(message); }
    var alt = ensureFileAlt(); alt.click(); alt.hidden = false;
  }

  // ---- API calls ------------------------------------------------------------
  // Auto-detection: runs whenever a photo is captured or selected. A sequence
  // number discards stale responses (photo replaced mid-flight), and the
  // in-flight check suppresses duplicate concurrent requests for the same image.
  var detectSeq = 0, detectInFlightFor = null;

  function renderDetectionResult(d) {
    // Shown immediately on the current step: mirrored on the Photo step (step 2)
    // and the Detection step (step 3), so no navigation is needed to see it.
    var html =
      "<div class='field'><b>Hazard:</b> " + (d.hazard_category || "—").replace(/_/g, " ") +
      "</div><div class='field'><b>Type:</b> " + (d.hazard_type || "—").replace(/_/g, " ") +
      "</div><div class='field'><b>Detected:</b> " + (d.detected ? "Yes" : "No") + "</div>" +
      "<div class='field'><b>Confidence:</b> " +
      (d.confidence != null ? Math.round(d.confidence * 100) + "%" : "—") + "</div>" +
      "<div class='field'><b>Validity:</b> " + (d.valid ? "Valid" : "Invalid") + "</div>";
    ["detection-result", "photo-detection-result"].forEach(function (id) {
      var el = $(id);
      if (el) { el.innerHTML = html; el.hidden = false; }
    });
  }

  function hideDetectionResult() {
    ["detection-result", "photo-detection-result"].forEach(function (id) {
      var el = $(id);
      if (el) { el.innerHTML = ""; el.hidden = true; }
    });
  }

  function startDetection() {
    if (!state.imageFile) return;
    if (detectInFlightFor === state.imageFile) return; // one request per image
    var seq = ++detectSeq;
    detectInFlightFor = state.imageFile;
    state.detection = null;

    clearError();
    detectError("");
    hideDetectionResult();
    $("detect-next").disabled = true;
    $("detect-loading").hidden = false;
    setDetectStatus("Analyzing photo…");

    var fd = new FormData();
    fd.append("image", state.imageFile, "report.jpg");
    fd.append("lat", state.lat); fd.append("lon", state.lon);

    function done(ok) {
      if (seq !== detectSeq) return; // stale — photo was replaced
      detectInFlightFor = null;
      $("detect-loading").hidden = true;
      setDetectStatus(ok ? "Detection complete." : "Detection failed.");
    }

    token().then(function (t) {
      if (!t) { done(false); return; }
      if (seq !== detectSeq) return;
      fd.append("_upload_token", t);
      return fetch("/api/detect", { method: "POST", body: fd });
    })
    .then(function (r) { return r && r.json ? r.json() : null; })
    .then(function (d) {
      if (seq !== detectSeq || !d) return; // stale or aborted
      if (!d.ok) {
        done(false);
        detectError(d.message || "Detection failed.");
        setDetectStatus(d.message || "Detection failed.");
        return;
      }
      state.detection = d;
      renderDetectionResult(d);
      if (!d.valid) {
        // Same mechanism as any other detection failure, but the reason is
        // specific: the server already distinguished "saw damage but not
        // confidently enough" from "saw no damage". Plain-language copy only --
        // no threshold names or detector internals are exposed.
        var msg = d.confidence != null
          ? "Detection confidence is below the 50% threshold. Please capture a clearer photo."
          : "No road damage detected — try a clearer photo of the road surface.";
        detectError(msg);
        setDetectStatus(msg);
        $("detect-next").disabled = true;
        done(false);
      } else {
        detectError("");
        $("detect-next").disabled = false;
        done(true);
      }
    })
    .catch(function () {
      if (seq !== detectSeq) return;
      done(false);
      detectError("Detection failed — check your connection and try again.");
      setDetectStatus("Detection failed — check your connection and try again.");
    });
  }

  var draftSummary = null;
  function generatePreview() {
    if (!state.detection || !state.detection.valid) {
      clearError();
      error("Detection has not passed for this photo — go back and use a clearer photo.");
      return;
    }
    $("generate-btn").disabled = true;
    var fd = new FormData();
    fd.append("image", state.imageFile, "report.jpg");
    fd.append("lat", state.lat); fd.append("lon", state.lon);
    fd.append("location_name", $("location_name").value.trim() ||
              $("location_name").placeholder);
    token().then(function (t) {
      if (!t) { $("generate-btn").disabled = false; return; }
      fd.append("_upload_token", t);
      return fetch("/api/report/preview", { method: "POST", body: fd });
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      $("generate-btn").disabled = false;
      if (!d.ok) { clearError(); error(d.message || "Could not generate the letter."); return; }
      state.draftId = d.report_id;
      draftSummary = d;
      token().then(function (t) {
        if (!t) return;
        $("preview-frame").src = "/api/report/" + d.report_id + "/pdf?_upload_token=" + t;
        $("preview-frame").hidden = false;
        $("preview-next").disabled = false;
      });
    });
  }

  function submitReport() {
    var fd = new FormData();
    $("submit-btn").disabled = true;
    token().then(function (t) {
      if (!t) { $("submit-btn").disabled = false; return; }
      fd.append("_upload_token", t);
      return fetch("/api/report/" + state.draftId + "/submit", { method: "POST", body: fd });
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) { $("submit-btn").disabled = false; error(d.message || "Submission failed."); return; }
      $("submit-summary").textContent =
        "Draft #" + d.report_id + " finalized and queued for review.";
      $("submit-done").hidden = false;
      $("submit-btn").hidden = true;
    });
  }

  // ---- Navigation wiring ----------------------------------------------------
  $("loc-button").addEventListener("click", requestGeo);
  $("location_name").addEventListener("input", function () { state.locationName = this.value; });
  $("loc-next").addEventListener("click", function () { showStep(2); });
  $("photo-back").addEventListener("click", function () { showStep(1); });
  $("take-photo").addEventListener("click", function () {
    if (window.isSecureContext && navigator.mediaDevices) startCamera();
    else openFilePicker("Camera needs HTTPS — selecting a file instead.");
  });
  $("use-file").addEventListener("click", function () { stopStream(); openFilePicker(); });
  $("capture-btn").addEventListener("click", onCapture);
  $("retake-btn").addEventListener("click", function () { stopStream(); startCamera(); });
  $("photo-next").addEventListener("click", function () { showStep(3); });
  $("detect-back").addEventListener("click", function () { showStep(2); });
  $("detect-next").addEventListener("click", function () { showStep(4); });
  $("preview-back").addEventListener("click", function () { showStep(3); });
  $("generate-btn").addEventListener("click", generatePreview);
  $("preview-next").addEventListener("click", function () { showStep(5); });
  $("submit-btn").addEventListener("click", submitReport);
  nav.forEach(function (b) { b.addEventListener("click", function () { showStep(+b.dataset.step); }); });

  if (!window.isSecureContext) $("secure-banner").hidden = false;
  if (navigator.geolocation) $("loc-button").hidden = false;
  initMap();
})();
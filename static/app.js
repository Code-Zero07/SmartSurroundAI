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

  // Tokenized upload: fetch a short-lived single-use upload token on load and
  // attach it to the form so its POST to /upload is accepted. Without a token
  // the server redirects to /login (protection, never a hard error).
  fetch("/upload/token")
    .then(function (resp) {
      if (!resp.ok) return;
      return resp.json();
    })
    .then(function (data) {
      if (data && data.ok && data.token) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "_upload_token";
        hidden.value = data.token;
        form.appendChild(hidden);
      }
    })
    .catch(function () { /* token optional: without it /upload redirects to /login */ });

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

    wireLocation();
    wireCamera();
    form.addEventListener("submit", onSubmit);

    // Swap native controls for enhanced ones LAST, so a throw anywhere above
    // leaves the native form fully intact.
    takePhotoBtn.hidden = false;
    useFileBtn.hidden = false;
    fileAnchor.hidden = true;
  }

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

  document.addEventListener("DOMContentLoaded", function () {
    try {
      init();
    } catch (err) {
      // Leave the native form untouched.
    }
  });
})();
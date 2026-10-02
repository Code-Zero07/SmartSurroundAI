# Dockerfile -- standalone road-damage ML inference service
#
# CPU-only, single-worker, no reload, no runtime package installation.
# The Flask app is NOT dockerized here (Phase 4) and there is no
# docker-compose yet.
#
# Build:  docker build -f ml/Dockerfile -t smart-surround-ml:1.0.0 .
# Run:    docker run --rm -p 8000:8000 smart-surround-ml:1.0.0

# Python 3.13.3 -- matches the verified local detector environment exactly, so
# the container resolves the same wheel set and reproduces the golden baseline.
FROM python:3.13.3-slim-bookworm

# Fail fast and write progress to stderr; no .pyc clutter in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- CRITICAL: make runtime package installation impossible -----------------
# ultralytics 8.4.133 patches PIL.Image.open so that any decode failure calls
# check_requirements("pi-heif") and LIVE-PIP-INSTALLS it. Phase 1 caught that
# mutating a live environment and upgrading Pillow 10.4.0 -> 12.3.0.
#
#   ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS=1
#       check_requirements() returns True immediately, before inspecting or
#       installing anything. This is the real lock.
#   YOLO_AUTOINSTALL=false
#       Gates the `if install and AUTOINSTALL` branch in check_requirements().
#       Belt and braces; also read at ultralytics import time.
#
# pi-heif is deliberately NOT a dependency -- every published release requires
# pillow>=11.1.0, which would break the verified pillow==10.4.0 pin, and
# inference never takes the Pillow/HEIC path (model.predict() decodes with
# OpenCV). Full reasoning in ml/requirements-ml.txt.
ENV YOLO_AUTOINSTALL=false \
    ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS=1 \
    ROAD_DAMAGE_MODEL_PATH=/app/models/road_damage_yolov8s.pt

WORKDIR /app

# --- Shared libraries for the GUI-linked opencv-python build -----------------
# `ultralytics` hard-requires `opencv-python>=4.6.0`, and that distribution links
# against libGL/libxcb/libglib. Importing cv2 on a headless slim image without
# them dies with `ImportError: libxcb.so.1: cannot open shared object file`
# (reproduced on build #8). opencv-python-headless is NOT a usable substitute
# here: pip installs both distributions into the same top-level `cv2/` directory
# because ultralytics still pulls in the GUI build, which then shadows it.
# So keep the dev environment's exact opencv-python==5.0.0.93 and provide the
# shared objects. No X server or display is involved -- only the loader.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer first so application edits do not invalidate the (slow,
# multi-hundred-MB) torch layer. --extra-index-url in requirements-ml.txt keeps
# PyTorch CPU-only; without it torch 2.14.0 from PyPI drags in ~GB of CUDA
# wheels on Linux.
COPY ml/requirements-ml.txt /app/requirements-ml.txt
RUN python -m pip install --no-cache-dir -r /app/requirements-ml.txt \
    && python -c "import torch, ultralytics, fastapi, uvicorn, multipart, cv2; \
print('torch', torch.__version__); print('ultralytics', ultralytics.__version__); \
print('cv2', cv2.__version__)" \
    && python -c "import sys, torch; \
leaked = [m for m in sys.modules if m.startswith('nvidia.')]; \
assert not leaked, 'CUDA runtime leaked into a CPU-only image: %s' % leaked; \
assert not torch.cuda.is_available(), 'torch reports CUDA available'; \
print('cpu-only confirmed: no nvidia modules, torch.cuda unavailable')" \
    && python -m pip check

# detector.py is COPIED VERBATIM. It is not edited, wrapped, patched or
# regenerated for the container -- the service imports the same module the
# Flask app uses today. ROAD_DAMAGE_MODEL_PATH above is belt-and-braces; the
# detector's own default (models/ next to its __file__) resolves identically
# here, so either mechanism lands on the same file.
#
# The container mirrors the repository layout so that `import detector` and the
# golden fixture path resolve with no path rewriting.
COPY detector.py /app/detector.py
COPY models/road_damage_yolov8s.pt /app/models/road_damage_yolov8s.pt
COPY ml/server.py /app/ml/server.py
COPY ml/parity_check.py /app/ml/parity_check.py
# The Phase 1 golden baseline, so parity can be proven against the container.
COPY tests/fixtures/road_damage_golden.json /app/tests/fixtures/road_damage_golden.json

# Sanity-check the weights landed intact (size only; hashing 89MB on every
# build is wasteful). The exact expected size is asserted, not guessed.
RUN python - <<'PY'
import os
p = "/app/models/road_damage_yolov8s.pt"
size = os.path.getsize(p)
assert size == 89569358, "unexpected model size: %d" % size
print("model ok:", p, size, "bytes")
PY

# Run as a non-root user. The inference path only needs a writable temp dir.
RUN useradd --create-home --uid 10001 mluser
USER mluser

EXPOSE 8000

# Exactly one worker, no --reload: the model is a process-wide singleton, so a
# second worker would mean a second resident copy (~90MB of weights plus torch
# allocator state) and split request traffic across two models.
# Model loading happens in the FastAPI lifespan startup hook, not per request.
# --app-dir /app/ml makes `server:app` resolve while `import detector` inside
# server.py resolves to /app/detector.py.
CMD ["python", "-m", "uvicorn", "server:app", \
     "--app-dir", "/app/ml", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log"]

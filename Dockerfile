# StarRailCopilot with cloud_web support (remote browser mode)
#
# This image runs SRC only — the browser runs on a SEPARATE machine
# on the same LAN, connected via CDP (Chrome DevTools Protocol).
#
# Multi-stage build: compile wheels in builder, copy to slim runtime.

# === Stage 1: Build wheels ===
FROM python:3.10-bookworm AS builder

WORKDIR /build

COPY requirements-in.txt .

# Install build deps + compile all wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ cmake \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --prefix=/install \
        -r requirements-in.txt selenium>=4.10.0

# === Stage 2: Runtime ===
FROM python:3.10-slim-bookworm

# Minimal runtime deps for OpenCV + CJK fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 \
    libxrender1 libfontconfig1 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY . .

# Create directories
RUN mkdir -p /app/config /app/log

EXPOSE 22267

ENV PYTHONPATH=/app

CMD ["python", "src.py"]
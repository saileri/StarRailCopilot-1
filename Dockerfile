# StarRailCopilot with cloud_web support (remote browser mode)
#
# This image runs SRC only — the browser runs on a SEPARATE machine
# on the same LAN, connected via CDP (Chrome DevTools Protocol).

# === Stage 1: Build wheels ===
FROM python:3.10-bookworm AS builder

WORKDIR /build
COPY requirements-in.txt .

# Install build deps: system libs for av/numpy + compile tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ cmake \
    pkg-config libavformat-dev libavcodec-dev libavdevice-dev \
    libavutil-dev libavfilter-dev libswscale-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --prefix=/install \
        -r requirements-in.txt selenium>=4.10.0

# === Stage 2: Runtime ===
FROM python:3.10-slim-bookworm

# Runtime deps: OpenCV + CJK fonts + FFmpeg libs for av
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 \
    libxrender1 libfontconfig1 fonts-noto-cjk \
    libavformat58 libavcodec58 libavdevice58 \
    libavutil56 libavfilter7 libswscale5 libswresample3 \
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
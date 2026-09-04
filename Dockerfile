# StarRailCopilot with cloud_web support (remote browser mode)
#
# This image runs SRC only — the browser runs on a SEPARATE machine
# on the same LAN, connected via CDP (Chrome DevTools Protocol).
#
# The browser machine (Windows/Mac/Linux) needs to start Chrome with:
#   chrome --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 \
#        --user-data-dir=/path/to/profile --force-device-scale-factor=1 \
#        --disable-blink-features=AutomationControlled \
#        --app=https://sr.mihoyo.com/cloud
#
# Then configure SRC with Browser_RemoteURL = "http://<browser-ip>:9222"

FROM python:3.10-slim-bookworm

# Minimal system deps for OpenCV + SRC
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libfontconfig1 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements-in.txt .
RUN pip install --no-cache-dir -r requirements-in.txt selenium>=4.10.0

# Copy application code
COPY . .

# Create directories
RUN mkdir -p /app/config /app/log

# WebUI port
EXPOSE 22267

ENV PYTHONPATH=/app

CMD ["python", "src.py"]
# StarRailCopilot with cloud_web support
# Runs SRC in headless Chrome mode for cloud HSR web version
#
# Usage:
#   docker run -d \
#     -p 22267:22267 \
#     -v ./config:/app/config \
#     -v ./browser_profile:/app/browser_profile \
#     starrail-copilot-cloud
#
# For remote browser mode (SRC on this machine, Chrome on another):
#   docker run -d \
#     -p 22267:22267 \
#     -v ./config:/app/config \
#     -e BROWSER_REMOTE_URL=http://192.168.31.x:9222 \
#     starrail-copilot-cloud

FROM python:3.10-slim-bookworm AS base

# Install system dependencies for OpenCV, Chrome, and Chinese fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    unzip \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libfontconfig1 \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for Testing (stable, headless-capable)
ENV CHROME_VERSION=140.0.7339.207
RUN wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip" -O /tmp/chrome.zip \
    && unzip /tmp/chrome.zip -d /opt/ \
    && rm /tmp/chrome.zip \
    && ln -sf /opt/chrome-linux64/chrome /usr/bin/google-chrome \
    && google-chrome --version

# Install chromedriver matching Chrome version
RUN wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
    && chmod +x /usr/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64

WORKDIR /app

# Install Python dependencies
COPY requirements-in.txt .
RUN pip install --no-cache-dir -r requirements-in.txt selenium>=4.10.0

# Copy application code
COPY . .

# Create directories for config and browser profile
RUN mkdir -p /app/config /app/browser_profile /app/log

# WebUI port
EXPOSE 22267

# Chrome remote debugging port (for remote browser mode)
EXPOSE 9222

ENV PYTHONPATH=/app
ENV DISPLAY=:0

# Default: run SRC with WebUI
CMD ["python", "src.py"]